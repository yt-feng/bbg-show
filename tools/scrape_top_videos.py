#!/usr/bin/env python3
"""Scrape Bloomberg /videos Top Videos links."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from download_bloomberg_video import (  # noqa: E402
    DEFAULT_PROXY_CACHE,
    DEFAULT_PROXY_TEST_URL,
    DEFAULT_SUBSCRIPTION,
    DEFAULT_SUBSCRIPTION_URL_FILE,
    FetchError,
    build_proxy_fetcher,
    bloomberg_brp_url,
    ensure_subscription,
    fetch_text_direct,
    safe_file_part,
)
from top_video_sources import (  # noqa: E402
    item_source_key,
    load_processed_source_keys,
    source_key,
)
from trump_filter import is_trump_related  # noqa: E402


DEFAULT_URL = "https://www.bloomberg.com/videos"
TOP_VIDEOS_XPATH = "/html/body/div[2]/div/div[2]/div[3]/main/section/section[1]/div"
VIDEO_PATH_RE = re.compile(r"/news/videos/\d{4}-\d{2}-\d{2}/[^\"'<>\s?#]+", re.IGNORECASE)
YOUTUBE_CHANNEL_ID = "UCIALMKvObZNtJ6AmdCLP7Lg"
YOUTUBE_FEED_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={YOUTUBE_CHANNEL_ID}"
ATOM_NS = "http://www.w3.org/2005/Atom"
YOUTUBE_NS = "http://www.youtube.com/xml/schemas/2015"
MEDIA_NS = "http://search.yahoo.com/mrss/"
YOUTUBE_NAMESPACES = {"atom": ATOM_NS, "yt": YOUTUBE_NS, "media": MEDIA_NS}
YOUTUBE_UNSUITABLE_TITLE_PATTERNS = (
    re.compile(r"(?:^|\s)#shorts?\b", re.IGNORECASE),
    re.compile(r"\blive\b", re.IGNORECASE),
    re.compile(r"\b(?:live\s*stream|livestream|full\s+(?:episode|show|broadcast))\b", re.IGNORECASE),
    re.compile(r"\b(?:episode|weekend)\b", re.IGNORECASE),
    re.compile(r"\b(?:pointed\s+)?(?:news\s+)?quiz\b", re.IGNORECASE),
    re.compile(r"\bheadlines?\b", re.IGNORECASE),
    re.compile(
        r"\|\s*(?:bloomberg\s+)?(?:surveillance|technology|daybreak|the\s+close|the\s+asia\s+trade|"
        r"balance\s+of\s+power|open\s+interest|businessweek(?:\s+daily)?)\b.*\b\d{1,2}/\d{1,2}",
        re.IGNORECASE,
    ),
)
YOUTUBE_CHAPTER_RE = re.compile(r"(?m)^\s*(?:\d{1,2}:){1,2}\d{2}\s*[-–—:]\s*\S")
TITLE_DURATION_PREFIX_RE = re.compile(r"^\s*duration\s*:\s*\d{1,2}:\d{2}(?::\d{2})?\s*", re.IGNORECASE)
TITLE_SOURCE_SUFFIX_RE = re.compile(
    r"\s+(?:the\s+china\s+show|bloomberg\s+(?:television|technology|brief|surveillance|"
    r"businessweek(?:\s+daily)?|this\s+weekend|wall\s+street\s+week|open\s+interest)|"
    r"daybreak(?::\s*[a-z]+)?|the\s+opening\s+trade)\s*$",
    re.IGNORECASE,
)


def log(message: str) -> None:
    print(f"[top-videos] {message}", flush=True)


def chrome_binary() -> str | None:
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    ]
    for candidate in candidates:
        if candidate.startswith("/") and Path(candidate).exists():
            return candidate
        if not candidate.startswith("/") and shutil_which(candidate):
            return candidate
    return None


def shutil_which(name: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        path = Path(directory) / name
        if path.exists() and os.access(path, os.X_OK):
            return str(path)
    return None


def clean_text(value: str) -> str:
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_title_key(value: str) -> str:
    value = clean_text(value).casefold()
    value = TITLE_DURATION_PREFIX_RE.sub("", value)
    value = value.split("|", 1)[0].strip()
    value = TITLE_SOURCE_SUFFIX_RE.sub("", value)
    return re.sub(r"[^a-z0-9]+", "", value)


def parse_youtube_datetime(value: str) -> datetime | None:
    value = clean_text(value)
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def youtube_channel_matches(value: str, expected_channel_id: str = YOUTUBE_CHANNEL_ID) -> bool:
    value = clean_text(value)
    return value in {expected_channel_id, expected_channel_id.removeprefix("UC")}


def youtube_entry_link(entry: ET.Element) -> str:
    for link in entry.findall("atom:link", YOUTUBE_NAMESPACES):
        if link.get("rel") == "alternate":
            return clean_text(link.get("href", ""))
    return ""


def unsuitable_youtube_entry(title: str, url: str, raw_description: str) -> str:
    if "/shorts/" in urlsplit(url).path.lower():
        return "shorts URL"
    for pattern in YOUTUBE_UNSUITABLE_TITLE_PATTERNS:
        if pattern.search(title):
            return f"title pattern: {pattern.pattern}"
    if len(YOUTUBE_CHAPTER_RE.findall(raw_description)) >= 3:
        return "multi-chapter/full-episode description"
    return ""


def parse_youtube_feed(
    xml_bytes: bytes,
    *,
    max_videos: int,
    existing_title_keys: set[str] | None = None,
    now: datetime | None = None,
    max_age_hours: int = 48,
    expected_channel_id: str = YOUTUBE_CHANNEL_ID,
    processed_source_keys: set[str] | None = None,
) -> list[dict[str, str]]:
    root = ET.fromstring(xml_bytes)
    feed_channel_id = root.findtext("yt:channelId", default="", namespaces=YOUTUBE_NAMESPACES)
    alternate_links = [
        clean_text(link.get("href", ""))
        for link in root.findall("atom:link", YOUTUBE_NAMESPACES)
        if link.get("rel") == "alternate"
    ]
    channel_link_matches = any(
        urlsplit(url).path.rstrip("/").endswith(f"/channel/{expected_channel_id}")
        for url in alternate_links
    )
    if not youtube_channel_matches(feed_channel_id, expected_channel_id) or not channel_link_matches:
        raise ValueError(f"YouTube feed did not match official Bloomberg channel {expected_channel_id}")

    if max_videos < 1:
        return []
    reference_time = now or datetime.now(timezone.utc)
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)
    reference_time = reference_time.astimezone(timezone.utc)
    cutoff = reference_time - timedelta(hours=max(1, max_age_hours))
    seen_ids: set[str] = set()
    seen_title_keys = set(existing_title_keys or set())
    processed_keys = processed_source_keys or set()
    candidates: list[dict[str, str]] = []

    for entry in root.findall("atom:entry", YOUTUBE_NAMESPACES):
        video_id = clean_text(entry.findtext("yt:videoId", default="", namespaces=YOUTUBE_NAMESPACES))
        channel_id = clean_text(entry.findtext("yt:channelId", default="", namespaces=YOUTUBE_NAMESPACES))
        title = clean_text(entry.findtext("atom:title", default="", namespaces=YOUTUBE_NAMESPACES))
        published = parse_youtube_datetime(
            entry.findtext("atom:published", default="", namespaces=YOUTUBE_NAMESPACES)
        )
        url = youtube_entry_link(entry)
        raw_description = entry.findtext(
            "media:group/media:description",
            default="",
            namespaces=YOUTUBE_NAMESPACES,
        )
        description = clean_text(raw_description)

        if not video_id or not title or not url or published is None:
            continue
        if channel_id != expected_channel_id:
            log(f"Skipping YouTube entry from unexpected channel {channel_id or 'unknown'}: {title}")
            continue
        if published < cutoff or published > reference_time + timedelta(hours=1):
            continue
        canonical_url = f"https://www.youtube.com/watch?v={video_id}"
        if source_key(canonical_url, video_id) in processed_keys:
            log(f"Skipping previously processed YouTube backup: {title}")
            continue
        unsuitable_reason = unsuitable_youtube_entry(title, url, raw_description)
        if unsuitable_reason:
            log(f"Skipping unsuitable YouTube backup ({unsuitable_reason}): {title}")
            continue
        if is_trump_related(canonical_url, title, description):
            log(f"Skipping sensitive-topic YouTube backup: {title}")
            continue

        title_key = normalize_title_key(title)
        if video_id in seen_ids or not title_key or title_key in seen_title_keys:
            log(f"Skipping duplicate YouTube backup: {title}")
            continue
        seen_ids.add(video_id)
        seen_title_keys.add(title_key)
        title_slug = safe_file_part(title)[:100]
        candidates.append({
            "url": canonical_url,
            "title": title,
            "slug": safe_file_part(f"youtube_{video_id}_{title_slug}"),
            "source": "youtube-backup",
            "youtube_id": video_id,
            "channel_id": channel_id,
            "published_at": published.isoformat(),
            "description": description,
        })

    candidates.sort(key=lambda item: item["published_at"], reverse=True)
    return candidates[:max_videos]


def fetch_youtube_feed(url: str = YOUTUBE_FEED_URL, timeout: int = 30) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.8",
            "User-Agent": "Mozilla/5.0 (compatible; bbg-show-top-videos/1.0)",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def normalize_url(base_url: str, href: str) -> str:
    href = href.strip().rstrip("\\")
    url = urljoin(base_url, html.unescape(href))
    parts = urlsplit(url)
    return urlunsplit((parts.scheme or "https", parts.netloc or "www.bloomberg.com", parts.path, "", ""))


def add_link(
    links: list[dict[str, str]],
    seen: set[str],
    url: str,
    title: str,
    processed_source_keys: set[str] | None = None,
) -> None:
    normalized = normalize_url(DEFAULT_URL, url)
    if "/news/videos/" not in urlsplit(normalized).path:
        return
    if normalized in seen:
        return
    seen.add(normalized)
    clean_title = clean_text(title) or title_from_url(normalized)
    if is_trump_related(normalized, clean_title):
        log(f"Skipping sensitive-topic top video: {clean_title or normalized}")
        return
    if source_key(normalized) in (processed_source_keys or set()):
        log(f"Skipping previously processed top video: {clean_title or normalized}")
        return
    links.append({
        "url": normalized,
        "title": clean_title,
        "slug": safe_file_part(title_from_url(normalized)),
        "source": "bloomberg",
    })


def title_from_url(url: str) -> str:
    slug = Path(urlsplit(url).path).name
    slug = re.sub(r"-?video$", "", slug, flags=re.IGNORECASE)
    return clean_text(slug.replace("-", " ")).title()


def direct_top_video_slice(
    links: list[dict[str, str]],
    max_videos: int,
    skip_leading: int,
    processed_source_keys: set[str] | None = None,
) -> list[dict[str, str]]:
    if skip_leading > 0 and len(links) >= max_videos + skip_leading:
        links = links[skip_leading:]
    processed_keys = processed_source_keys or set()
    selected: list[dict[str, str]] = []
    for item in links:
        if item_source_key(item) in processed_keys:
            log(f"Skipping previously processed top video: {item.get('title') or item.get('url')}")
            continue
        selected.append(item)
        if len(selected) >= max_videos:
            break
    return selected


def extract_links_from_html(
    text: str,
    base_url: str,
    max_videos: int,
    *,
    skip_leading: int = 4,
    processed_source_keys: set[str] | None = None,
) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    anchor_re = re.compile(
        r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    for match in anchor_re.finditer(text):
        href = html.unescape(match.group(1))
        if not VIDEO_PATH_RE.search(href):
            continue
        label = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", match.group(2))
        label = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", label)
        label = re.sub(r"(?s)<[^>]+>", " ", label)
        add_link(links, seen, normalize_url(base_url, href), label)

    for match in VIDEO_PATH_RE.finditer(html.unescape(text)):
        add_link(links, seen, normalize_url(base_url, match.group(0)), "")
    return direct_top_video_slice(
        links,
        max_videos,
        skip_leading,
        processed_source_keys,
    )


class WebSocketClient:
    def __init__(self, ws_url: str) -> None:
        parts = urlsplit(ws_url)
        if parts.scheme != "ws" or not parts.hostname or not parts.port:
            raise RuntimeError(f"Unsupported websocket URL: {ws_url}")
        self.path = parts.path + (f"?{parts.query}" if parts.query else "")
        self.sock = socket.create_connection((parts.hostname, parts.port), timeout=20)
        self.sock.settimeout(30)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {parts.hostname}:{parts.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        response = self._read_http_response()
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"WebSocket upgrade failed: {response[:200]!r}")
        self.next_id = 1

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def _read_http_response(self) -> bytes:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data)

    def _recv_exact(self, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = self.sock.recv(size - len(chunks))
            if not chunk:
                raise RuntimeError("WebSocket closed")
            chunks.extend(chunk)
        return bytes(chunks)

    def send_json(self, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        header = bytearray([0x81])
        length = len(raw)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(raw))
        self.sock.sendall(bytes(header) + masked)

    def recv_json(self) -> dict[str, Any]:
        while True:
            first, second = self._recv_exact(2)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length)
            if masked:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x8:
                raise RuntimeError("WebSocket closed")
            if opcode == 0x9:
                continue
            if opcode in {0x1, 0x2}:
                return json.loads(payload.decode("utf-8"))

    def command(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        command_id = self.next_id
        self.next_id += 1
        self.send_json({"id": command_id, "method": method, "params": params or {}})
        while True:
            message = self.recv_json()
            if message.get("id") == command_id:
                if "error" in message:
                    raise RuntimeError(f"CDP {method} failed: {message['error']}")
                return message.get("result", {})


def chrome_page_ws(debug_port: int, timeout: int = 30) -> str:
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{debug_port}/json/list", timeout=2) as response:
                pages = json.loads(response.read())
            for page in pages:
                if page.get("type") == "page" and page.get("webSocketDebuggerUrl"):
                    return str(page["webSocketDebuggerUrl"])
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            last_error = str(exc)
        time.sleep(0.5)
    raise RuntimeError(f"Could not locate Chrome page websocket: {last_error}")


def allocate_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def extract_links_with_headless_chrome(
    url: str,
    xpath: str,
    max_videos: int,
    wait_seconds: int,
    processed_source_keys: set[str] | None = None,
) -> list[dict[str, str]]:
    binary = chrome_binary()
    if not binary:
        raise RuntimeError("No Chrome/Chromium binary found")

    profile_seed = hashlib.sha1(f"{url}-{time.time()}".encode()).hexdigest()[:12]
    profile_dir = Path("/tmp") / f"bbg-top-videos-chrome-{profile_seed}"
    debug_port = allocate_local_port()
    command = [
        binary,
        "--headless=new",
        "--disable-gpu",
        "--disable-background-networking",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-sync",
        "--disable-translate",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1440,1200",
        f"--remote-debugging-port={debug_port}",
        f"--user-data-dir={profile_dir}",
        url,
    ]
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ws = WebSocketClient(chrome_page_ws(debug_port))
        try:
            ws.command("Page.enable")
            ws.command("Runtime.enable")
            time.sleep(wait_seconds)
            excluded_urls = {
                key.removeprefix("url:")
                for key in (processed_source_keys or set())
                if key.startswith("url:")
            }
            expression = top_videos_js(xpath, max_videos, excluded_urls)
            result = ws.command("Runtime.evaluate", {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
            })
        finally:
            ws.close()
        value = result.get("result", {}).get("value", "{}")
        parsed = json.loads(value)
        return normalize_browser_links(
            parsed.get("links", []),
            max_videos,
            processed_source_keys=processed_source_keys,
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def top_videos_js(
    xpath: str,
    max_videos: int,
    excluded_urls: set[str] | None = None,
) -> str:
    return f"""
(async function () {{
  const xpath = {json.dumps(xpath)};
  const maxVideos = {int(max_videos)};
  const excludedUrls = new Set({json.dumps(sorted(excluded_urls or set()))});
  const node = document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
  const root = node || document;
  const seen = new Map();
  function addLinks() {{
    Array.from(root.querySelectorAll('a[href]')).forEach((a) => {{
      const href = a.href || '';
      if (!/\\/news\\/videos\\//.test(href)) return;
      const normalized = new URL(href, location.href);
      normalized.search = '';
      normalized.hash = '';
      if (normalized.hostname === 'bloomberg.com' || normalized.hostname === 'www.bloomberg.com') {{
        normalized.protocol = 'https:';
        normalized.hostname = 'www.bloomberg.com';
        normalized.port = '';
      }}
      normalized.pathname = normalized.pathname.replace(/\\/$/, '') || '/';
      if (excludedUrls.has(normalized.toString())) return;
      const text = (a.innerText || a.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
      const canonicalHref = normalized.toString();
      if (!seen.has(canonicalHref)) seen.set(canonicalHref, {{ url: canonicalHref, title: text }});
    }});
  }}
  function scrollCandidates() {{
    const nodes = [root].concat(Array.from(root.querySelectorAll('*')));
    return nodes.filter((item) => item && item.scrollWidth > item.clientWidth + 20);
  }}
  addLinks();
  const candidates = scrollCandidates();
  for (let step = 0; step < 10 && seen.size < maxVideos; step += 1) {{
    candidates.forEach((item) => {{
      item.scrollLeft = Math.min(item.scrollWidth, item.scrollLeft + Math.max(300, item.clientWidth * 0.8));
      item.dispatchEvent(new Event('scroll', {{ bubbles: true }}));
    }});
    await new Promise((resolve) => setTimeout(resolve, 500));
    addLinks();
  }}
  return JSON.stringify({{
    url: location.href,
    title: document.title,
    ready: document.readyState,
    foundXPath: Boolean(node),
    links: Array.from(seen.values()).slice(0, maxVideos)
  }});
}})()
"""


def normalize_browser_links(
    items: list[dict[str, Any]],
    max_videos: int,
    *,
    processed_source_keys: set[str] | None = None,
) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        add_link(
            links,
            seen,
            str(item.get("url", "")),
            str(item.get("title", "")),
            processed_source_keys,
        )
        if len(links) >= max_videos:
            break
    return links


def fetch_text_proxy(url: str, work_dir: Path) -> str:
    args = argparse.Namespace(
        subscription=DEFAULT_SUBSCRIPTION,
        subscription_url="",
        subscription_url_file=DEFAULT_SUBSCRIPTION_URL_FILE,
        refresh_subscription=False,
        proxy_cache=DEFAULT_PROXY_CACHE,
        proxy_test_url=DEFAULT_PROXY_TEST_URL,
        google_doh=True,
        url=url,
    )
    subscription = ensure_subscription(args)
    fetcher, _proxy = build_proxy_fetcher(args, subscription, work_dir)
    return fetcher(url, "top_videos_page", timeout=120)


def scrape(
    url: str,
    xpath: str,
    max_videos: int,
    method: str,
    wait_seconds: int,
    direct_skip_leading: int,
    work_dir: Path,
    processed_source_keys: set[str] | None = None,
) -> tuple[str, list[dict[str, str]]]:
    errors: list[str] = []
    if method in {"auto", "direct"}:
        try:
            log(f"Trying direct page fetch: {url}")
            text = fetch_text_direct(url, timeout=90)
            links = extract_links_from_html(
                text,
                url,
                max_videos,
                skip_leading=direct_skip_leading,
                processed_source_keys=processed_source_keys,
            )
            if links:
                log(f"Direct page fetch found {len(links)} video link(s)")
                return "direct", links
            errors.append("direct fetch returned no video links")
            log("Direct page fetch returned no video links")
        except FetchError as exc:
            errors.append(f"direct failed: {exc}")
            log(f"Direct page fetch failed: {exc}")

    if method in {"auto", "brp"}:
        brp_url = bloomberg_brp_url(url)
        if brp_url != url:
            try:
                log(f"Trying BRP background page fetch: {brp_url}")
                text = fetch_text_direct(brp_url, timeout=45)
                links = extract_links_from_html(
                    text,
                    url,
                    max_videos,
                    skip_leading=direct_skip_leading,
                    processed_source_keys=processed_source_keys,
                )
                if links:
                    log(f"BRP background page fetch found {len(links)} video link(s)")
                    return "brp", links
                errors.append("brp fetch returned no video links")
                log("BRP background page fetch returned no video links")
            except FetchError as exc:
                errors.append(f"brp failed: {exc}")
                log(f"BRP background page fetch failed: {exc}")

    if method in {"auto", "proxy"}:
        try:
            log("Trying proxy page fetch fallback")
            text = fetch_text_proxy(url, work_dir)
            links = extract_links_from_html(
                text,
                url,
                max_videos,
                skip_leading=direct_skip_leading,
                processed_source_keys=processed_source_keys,
            )
            if links:
                log(f"Proxy page fetch found {len(links)} video link(s)")
                return "proxy", links
            errors.append("proxy fetch returned no video links")
            log("Proxy page fetch returned no video links")
        except (Exception, SystemExit) as exc:
            errors.append(f"proxy failed: {exc}")
            log(f"Proxy page fetch failed: {exc}")

    if method in {"auto", "chrome"}:
        try:
            log("Trying headless Chrome XPath fallback")
            links = extract_links_with_headless_chrome(
                url,
                xpath,
                max_videos,
                wait_seconds,
                processed_source_keys,
            )
            if links:
                log(f"Headless Chrome found {len(links)} video link(s)")
                return "chrome", links
            errors.append("headless Chrome returned no video links")
            log("Headless Chrome returned no video links")
        except Exception as exc:  # noqa: BLE001 - surface all scrape diagnostics
            errors.append(f"chrome failed: {exc}")
            log(f"Headless Chrome failed: {exc}")

    raise SystemExit("Could not scrape Top Videos links: " + " | ".join(errors))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--xpath", default=TOP_VIDEOS_XPATH)
    parser.add_argument("--max-videos", type=int, default=9)
    parser.add_argument("--method", choices=("auto", "direct", "brp", "proxy", "chrome"), default="auto")
    parser.add_argument("--wait-seconds", type=int, default=8)
    parser.add_argument("--work-dir", type=Path, default=Path("work/top-videos/scrape"))
    parser.add_argument(
        "--direct-skip-leading",
        type=int,
        default=4,
        help="When direct HTML/RSC extraction sees enough links, skip leading hero videos before Top Videos.",
    )
    parser.add_argument(
        "--youtube-backup-videos",
        type=int,
        default=0,
        help="Append this many fresh videos from Bloomberg Television's official YouTube Atom feed.",
    )
    parser.add_argument("--youtube-feed-url", default=YOUTUBE_FEED_URL)
    parser.add_argument("--youtube-max-age-hours", type=int, default=48)
    parser.add_argument(
        "--processed-sources",
        type=Path,
        default=Path("rendered-clips/top-videos/processed_sources.json"),
        help="Persistent successful-source ledger used for cross-run deduplication.",
    )
    parser.add_argument(
        "--history-root",
        type=Path,
        default=Path("rendered-clips/top-videos"),
        help="Top Videos root whose retained dated summaries seed successful-source history.",
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    if args.max_videos < 1:
        raise SystemExit("--max-videos must be at least 1")
    if args.youtube_backup_videos < 0:
        raise SystemExit("--youtube-backup-videos must be at least 0")
    if args.youtube_max_age_hours < 1:
        raise SystemExit("--youtube-max-age-hours must be at least 1")

    try:
        processed_keys = load_processed_source_keys(args.processed_sources, args.history_root)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    log(f"Loaded {len(processed_keys)} previously successful source identity/identities")

    primary_error = ""
    try:
        method, links = scrape(
            args.url,
            args.xpath,
            args.max_videos,
            args.method,
            args.wait_seconds,
            args.direct_skip_leading,
            args.work_dir,
            processed_keys,
        )
    except SystemExit as exc:
        if args.youtube_backup_videos < 1:
            raise
        primary_error = str(exc)
        method = "youtube-backup-only"
        links = []
        log(f"Bloomberg Top Videos scrape failed; using YouTube backup only: {primary_error}")

    links = [item for item in links if item_source_key(item) not in processed_keys]
    links = links[: args.max_videos]
    primary_count = len(links)
    youtube_links: list[dict[str, str]] = []
    youtube_error = ""
    youtube_feed_available = False
    if args.youtube_backup_videos:
        try:
            xml_bytes = fetch_youtube_feed(args.youtube_feed_url)
            youtube_feed_available = True
            existing_title_keys = {
                key
                for item in links
                if (key := normalize_title_key(item.get("title", "")))
            }
            youtube_links = parse_youtube_feed(
                xml_bytes,
                max_videos=args.youtube_backup_videos,
                existing_title_keys=existing_title_keys,
                max_age_hours=args.youtube_max_age_hours,
                processed_source_keys=processed_keys,
            )
            links.extend(youtube_links)
            log(f"Appended {len(youtube_links)} official YouTube backup video(s)")
        except (ET.ParseError, OSError, ValueError) as exc:
            youtube_error = str(exc)
            log(f"YouTube backup feed unavailable: {youtube_error}")

    if not links and not youtube_feed_available:
        details = " | ".join(detail for detail in (primary_error, youtube_error) if detail)
        raise SystemExit("No eligible Bloomberg Top Videos found" + (f": {details}" if details else ""))

    payload = {
        "source_url": args.url,
        "source_xpath": args.xpath,
        "scrape_method": method,
        "scraped_at": int(time.time()),
        "primary_count": primary_count,
        "youtube_backup_feed_url": args.youtube_feed_url if args.youtube_backup_videos else "",
        "youtube_backup_requested": args.youtube_backup_videos,
        "youtube_backup_count": len(youtube_links),
        "selection_status": "selected" if links else "no_eligible_videos",
        "count": len(links),
        "videos": links,
    }
    if primary_error:
        payload["primary_error"] = primary_error
    if youtube_error:
        payload["youtube_backup_error"] = youtube_error
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"Scraped {primary_count} Bloomberg Top Videos via {method} and appended "
        f"{len(youtube_links)} YouTube backup video(s): {args.out}",
        flush=True,
    )
    for index, item in enumerate(links, start=1):
        print(f"  {index:02d}. {item['title']} -> {item['url']}", flush=True)


if __name__ == "__main__":
    main()
