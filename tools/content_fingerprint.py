#!/usr/bin/env python3
"""Stable, compact text fingerprints for duplicate-source and duplicate-clip checks.

The fingerprint deliberately excludes titles and other mutable presentation
metadata.  It uses normalized transcript/subtitle words, five-word shingles, a
128-bit SimHash, and a bottom-k (KMV) sketch.  The conservative comparison
thresholds are intentional: a common short programme intro must not suppress a
new video.
"""

from __future__ import annotations

import hashlib
import math
import unicodedata
from typing import Any, Iterable


FINGERPRINT_VERSION = 1
SHINGLE_SIZE = 5
DEFAULT_SKETCH_SIZE = 128
MAX_SKETCH_SIZE = 4096

# Exact duplicates still need substantive text; this excludes stings, greetings,
# and short boilerplate.  Approximate matching is deliberately stricter.
MIN_EXACT_CLIP_TOKENS = 16
MIN_EXACT_CLIP_SHINGLES = 10
MIN_EXACT_FULL_SOURCE_TOKENS = 45
MIN_EXACT_FULL_SOURCE_SHINGLES = 36
MIN_SIMILAR_CLIP_TOKENS = 45
MIN_SIMILAR_CLIP_SHINGLES = 36
MIN_FULL_SOURCE_TOKENS = 240
MIN_FULL_SOURCE_SHINGLES = 160

HEX_64_LENGTH = 16
HEX_128_LENGTH = 32
HEX_256_LENGTH = 64


class FingerprintError(ValueError):
    """Raised when a fingerprint or fingerprint input is malformed."""


def _is_word_character(char: str) -> bool:
    return unicodedata.category(char)[:1] in {"L", "M", "N"}


def _is_cjk(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x3134F
    )


def normalize_words(text: str) -> list[str]:
    """Return NFKC/lower-cased word tokens, with CJK characters tokenized.

    Apostrophes inside a word are retained.  Punctuation, spacing, case, and
    full-width forms therefore do not change a fingerprint.
    """

    if not isinstance(text, str):
        raise FingerprintError("text must be a string")
    normalized = unicodedata.normalize("NFKC", text).lower().replace("\u2019", "'")
    words: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        token = "".join(buffer).strip("'")
        buffer.clear()
        if token:
            words.append(token)

    for char in normalized:
        if _is_cjk(char):
            flush()
            words.append(char)
        elif _is_word_character(char):
            buffer.append(char)
        elif char == "'" and buffer and buffer[-1] != "'":
            buffer.append(char)
        else:
            flush()
    flush()
    return words


def _shingles(words: list[str]) -> set[bytes]:
    if len(words) < SHINGLE_SIZE:
        return set()
    return {
        "\x1f".join(words[index : index + SHINGLE_SIZE]).encode("utf-8")
        for index in range(len(words) - SHINGLE_SIZE + 1)
    }


def _simhash128(shingles: Iterable[bytes]) -> str:
    weights = [0] * 128
    count = 0
    for shingle in shingles:
        count += 1
        value = int.from_bytes(hashlib.blake2b(shingle, digest_size=16).digest(), "big")
        for bit in range(128):
            weights[bit] += 1 if value & (1 << bit) else -1
    if not count:
        return "0" * HEX_128_LENGTH
    result = 0
    for bit, weight in enumerate(weights):
        if weight >= 0:
            result |= 1 << bit
    return f"{result:032x}"


def fingerprint_text(text: str, sketch_size: int = DEFAULT_SKETCH_SIZE) -> dict[str, Any]:
    """Build a JSON-serializable content fingerprint for *text*."""

    if type(sketch_size) is not int or not 1 <= sketch_size <= MAX_SKETCH_SIZE:
        raise FingerprintError(f"sketch_size must be an integer from 1 to {MAX_SKETCH_SIZE}")
    words = normalize_words(text)
    normalized = " ".join(words).encode("utf-8")
    shingles = _shingles(words)
    hashes = sorted(
        int.from_bytes(hashlib.blake2b(shingle, digest_size=8).digest(), "big")
        for shingle in shingles
    )
    result = {
        "version": FINGERPRINT_VERSION,
        "token_count": len(words),
        "unique_shingle_count": len(shingles),
        "normalized_sha256": hashlib.sha256(normalized).hexdigest(),
        "simhash128": _simhash128(shingles),
        "bottom_k": [f"{value:016x}" for value in hashes[:sketch_size]],
    }
    return validate_fingerprint(result)


def _valid_hex(value: Any, length: int) -> bool:
    if not isinstance(value, str) or len(value) != length or value != value.lower():
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def validate_fingerprint(value: Any) -> dict[str, Any]:
    """Validate and return a fingerprint loaded from an untrusted JSON ledger."""

    keys = {
        "version",
        "token_count",
        "unique_shingle_count",
        "normalized_sha256",
        "simhash128",
        "bottom_k",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise FingerprintError("fingerprint has invalid keys")
    if type(value["version"]) is not int or value["version"] != FINGERPRINT_VERSION:
        raise FingerprintError(f"fingerprint.version must be integer {FINGERPRINT_VERSION}")
    for key in ("token_count", "unique_shingle_count"):
        if type(value[key]) is not int or value[key] < 0:
            raise FingerprintError(f"fingerprint.{key} must be a non-negative integer")
    maximum_shingles = max(0, value["token_count"] - SHINGLE_SIZE + 1)
    if value["unique_shingle_count"] > maximum_shingles:
        raise FingerprintError("fingerprint.unique_shingle_count exceeds the possible shingle count")
    if not _valid_hex(value["normalized_sha256"], HEX_256_LENGTH):
        raise FingerprintError("fingerprint.normalized_sha256 must be 64 lowercase hex characters")
    if not _valid_hex(value["simhash128"], HEX_128_LENGTH):
        raise FingerprintError("fingerprint.simhash128 must be 32 lowercase hex characters")
    bottom_k = value["bottom_k"]
    if not isinstance(bottom_k, list) or len(bottom_k) > MAX_SKETCH_SIZE:
        raise FingerprintError("fingerprint.bottom_k must be a bounded array")
    if any(not _valid_hex(item, HEX_64_LENGTH) for item in bottom_k):
        raise FingerprintError("fingerprint.bottom_k entries must be 16 lowercase hex characters")
    if bottom_k != sorted(set(bottom_k)):
        raise FingerprintError("fingerprint.bottom_k must be sorted and unique")
    if len(bottom_k) > value["unique_shingle_count"]:
        raise FingerprintError("fingerprint.bottom_k is longer than unique_shingle_count")
    if value["unique_shingle_count"] and not bottom_k:
        raise FingerprintError("fingerprint.bottom_k cannot be empty when shingles exist")
    return value


def _hamming_distance(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count() if hasattr(int, "bit_count") else bin(
        int(left, 16) ^ int(right, 16)
    ).count("1")


def _kmv_jaccard(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_values = set(left["bottom_k"])
    right_values = set(right["bottom_k"])
    if not left_values and not right_values:
        return 1.0
    if not left_values or not right_values:
        return 0.0
    sample_size = min(len(left_values), len(right_values))
    union_sample = sorted(left_values | right_values)[:sample_size]
    return sum(item in left_values and item in right_values for item in union_sample) / len(union_sample)


def _kmv_containment(left: dict[str, Any], right: dict[str, Any]) -> float:
    """Estimate how much of the smaller shingle set occurs in the larger one.

    The shared hash cutoff keeps differently sized bottom-k sketches comparable.
    For normal 30-90 second clips the sketches contain every unique shingle, so
    this is the exact five-word-shingle containment score.
    """

    left_values = {int(value, 16) for value in left["bottom_k"]}
    right_values = {int(value, 16) for value in right["bottom_k"]}
    if not left_values or not right_values:
        return 0.0
    cutoff = min(max(left_values), max(right_values))
    left_sample = {value for value in left_values if value <= cutoff}
    right_sample = {value for value in right_values if value <= cutoff}
    denominator = min(len(left_sample), len(right_sample))
    if not denominator:
        return 0.0
    return len(left_sample & right_sample) / denominator


def _duration_close(left: Any, right: Any) -> bool:
    for name, value in (("duration_a", left), ("duration_b", right)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise FingerprintError(f"{name} must be a finite non-negative number")
    if left == 0 or right == 0:
        return True
    maximum = max(float(left), float(right))
    return abs(float(left) - float(right)) <= max(60.0, maximum * 0.04)


def same_full_source(a: Any, b: Any, duration_a: float, duration_b: float) -> bool:
    """Return whether two substantial transcripts represent the same source."""

    left = validate_fingerprint(a)
    right = validate_fingerprint(b)
    if not _duration_close(duration_a, duration_b):
        return False
    if (
        min(left["token_count"], right["token_count"])
        >= MIN_EXACT_FULL_SOURCE_TOKENS
        and min(left["unique_shingle_count"], right["unique_shingle_count"])
        >= MIN_EXACT_FULL_SOURCE_SHINGLES
        and left["normalized_sha256"] == right["normalized_sha256"]
    ):
        return True
    if min(left["token_count"], right["token_count"]) < MIN_FULL_SOURCE_TOKENS:
        return False
    if min(left["unique_shingle_count"], right["unique_shingle_count"]) < MIN_FULL_SOURCE_SHINGLES:
        return False
    token_ratio = min(left["token_count"], right["token_count"]) / max(
        left["token_count"], right["token_count"]
    )
    return (
        token_ratio >= 0.86
        and _kmv_jaccard(left, right) >= 0.72
        and _hamming_distance(left["simhash128"], right["simhash128"]) <= 22
    )


def same_clip(a: Any, b: Any) -> bool:
    """Return whether two subtitle bodies are the same published clip."""

    left = validate_fingerprint(a)
    right = validate_fingerprint(b)
    if (
        min(left["token_count"], right["token_count"]) >= MIN_EXACT_CLIP_TOKENS
        and min(left["unique_shingle_count"], right["unique_shingle_count"])
        >= MIN_EXACT_CLIP_SHINGLES
        and left["normalized_sha256"] == right["normalized_sha256"]
    ):
        return True
    if min(left["token_count"], right["token_count"]) < MIN_SIMILAR_CLIP_TOKENS:
        return False
    if min(left["unique_shingle_count"], right["unique_shingle_count"]) < MIN_SIMILAR_CLIP_SHINGLES:
        return False
    token_ratio = min(left["token_count"], right["token_count"]) / max(
        left["token_count"], right["token_count"]
    )
    return (
        token_ratio >= 0.60
        and _kmv_containment(left, right) >= 0.86
        and _kmv_jaccard(left, right) >= 0.50
    )


def _clip_text(clip: Any) -> str:
    if not isinstance(clip, dict):
        return ""
    subtitles = clip.get("subtitles")
    if not isinstance(subtitles, list):
        return ""
    # English reflects the source audio and is not affected by Chinese editorial
    # rewrites.  Fall back only when an entire clip has no English subtitles.
    for key in ("en", "text", "zh"):
        values = [
            item[key].strip()
            for item in subtitles
            if isinstance(item, dict) and isinstance(item.get(key), str) and item[key].strip()
        ]
        if values:
            return " ".join(values)
    return ""


def fingerprints_from_plan(plan: Any) -> list[dict[str, Any]]:
    """Return one subtitle-body fingerprint for each clip in plan order."""

    if not isinstance(plan, dict) or not isinstance(plan.get("clips"), list):
        raise FingerprintError("highlight plan must contain a clips array")
    return [fingerprint_text(_clip_text(clip)) for clip in plan["clips"]]
