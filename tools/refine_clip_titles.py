#!/usr/bin/env python3
"""Refine rendered clip titles with DeepSeek for stronger short-video hooks."""

from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plan_speaker_full import clean_text, normalize_highlights, safe_zh  # noqa: E402
from plan_speaker_highlights import ask_deepseek  # noqa: E402


TITLE_LINE_LIMITS = (10, 12, 14)
TITLE_MAX_CHARS = 36
DEFAULT_BATCH_SIZE = 8
DEFAULT_MAX_SUBTITLES = 24
DEFAULT_LOOKUP_MAX_QUERIES = 8
DEFAULT_LOOKUP_RESULTS_PER_QUERY = 3
CHINA_CONTEXT_RE = re.compile(
    r"(中国|中资|中企|中概|内地|国内|人民币|楼市|房价|房地产|地产|A股|港股|"
    r"China|Chinese|Hong Kong|renminbi|yuan|property|housing|real estate)",
    re.IGNORECASE,
)
CHINA_NEGATIVE_FRAMING_REPLACEMENTS = (
    (re.compile(r"(?:外资|资金|资本)(?:正)?(?:集体)?逃离(中国资产|中国市场|A股|港股|中概股?)"), r"\1再定价"),
    (re.compile(r"(?:唱衰|看空)(中国(?:资产|市场|经济)?)"), r"\1信心修复"),
    (re.compile(r"(?:崩溃|崩盘|塌了|垮了)"), "承压"),
    (re.compile(r"(?:完了|没救了?|不行了?|药丸)"), "承压"),
    (re.compile(r"(?:被抛弃|被放弃|遭抛弃)"), "被重新定价"),
    (re.compile(r"(?:外资逃离|资金逃离|集体逃离)"), "资金再配置"),
    (re.compile(r"(?:惨败|失败|输麻了)"), "遇挑战"),
    (re.compile(r"(?:很惨|太惨|惨了)"), "承压"),
)
PUBLIC_SEARCH_URL = "https://lite.duckduckgo.com/lite/"
PUBLIC_SEARCH_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
TITLE_BADGE_PATTERNS = [
    re.compile(r"[【\[]\s*(?:彭博社?|Bloomberg)?\s*独家\s*[】\]]\s*", re.IGNORECASE),
    re.compile(r"^(?:彭博社?|Bloomberg)?\s*独家\s*[：:｜|\-、\s]*", re.IGNORECASE),
    re.compile(r"\bBloomberg\s+Exclusive\b\s*[：:｜|\-、\s]*", re.IGNORECASE),
    re.compile(r"彭博社?独家\s*[：:｜|\-、\s]*"),
]
GENERIC_ENTITY_STOPWORDS = {
    "Bloomberg",
    "China",
    "Chinese",
    "Asia",
    "Asian",
    "Market",
    "Markets",
    "Video",
    "Videos",
    "News",
    "Show",
    "Top",
    "The",
    "A",
    "An",
    "And",
    "Of",
    "For",
    "To",
    "In",
    "On",
    "With",
    "From",
    "Says",
    "Said",
    "Chief",
    "Economist",
    "Strategist",
    "Managing",
    "Partner",
    "Officer",
    "CIO",
    "CEO",
}
ENTITY_CANDIDATE_REJECT_WORDS = {
    "Says",
    "Said",
    "Sees",
    "See",
    "Tied",
    "Weak",
    "Strong",
    "May",
    "Might",
    "Have",
    "Has",
    "Had",
    "Bottomed",
    "Bottoming",
    "Drops",
    "Drop",
    "Rises",
    "Rise",
    "Falls",
    "Fall",
    "After",
    "Before",
    "Why",
    "How",
    "What",
    "When",
}
COMMON_TRADITIONAL_TO_SIMPLIFIED = str.maketrans({
    "灝": "灏",
    "蓮": "莲",
    "華": "华",
    "資": "资",
    "產": "产",
    "國": "国",
    "際": "际",
    "證": "证",
    "券": "券",
    "經": "经",
    "濟": "济",
    "學": "学",
    "師": "师",
    "總": "总",
    "監": "监",
    "顧": "顾",
    "問": "问",
    "寶": "宝",
    "豐": "丰",
    "銀": "银",
    "聯": "联",
    "儲": "储",
    "壽": "寿",
    "萬": "万",
    "億": "亿",
    "臺": "台",
    "台": "台",
})


def strip_title_badges(text: str) -> str:
    value = clean_text(text)
    for pattern in TITLE_BADGE_PATTERNS:
        value = pattern.sub("", value)
    return clean_text(value.strip(" ：:｜|-、[]【】"))


def compact_text(text: str, max_chars: int) -> str:
    value = safe_zh(to_simplified_common(strip_title_badges(text)))
    value = value.replace("：", "").replace(":", "")
    value = re.sub(r"^(?:标题|观点|看点|结论)\s*[：:]\s*", "", value)
    value = value.strip(" ，,。；;、｜|-")
    if len(value) <= max_chars:
        return value
    return value[:max_chars].rstrip(" ，,。；;、｜|-")


def compact_title(text: str) -> str:
    value = safe_zh(to_simplified_common(strip_title_badges(text)))
    value = re.sub(r"\s+", "", value)
    value = value.strip(" ，,。；;、｜|-")
    if len(value) <= TITLE_MAX_CHARS:
        return value
    return value[:TITLE_MAX_CHARS].rstrip(" ，,。；;、｜|-")


def to_simplified_common(text: str) -> str:
    return text.translate(COMMON_TRADITIONAL_TO_SIMPLIFIED)


def clip_duration(clip: dict[str, Any]) -> float:
    try:
        return round(float(clip.get("end", 0)) - float(clip.get("start", 0)), 1)
    except (TypeError, ValueError):
        return 0.0


def is_china_related_clip(clip: dict[str, Any]) -> bool:
    fields = [
        str(clip.get("title", "")),
        str(clip.get("speaker_context", "")),
        str(clip.get("source_title", "")),
    ]
    for subtitle in clip.get("subtitles", [])[:8]:
        if isinstance(subtitle, dict):
            fields.append(str(subtitle.get("zh", "")))
            fields.append(str(subtitle.get("en", "")))
    return bool(CHINA_CONTEXT_RE.search(" ".join(fields)))


def china_safe_title_text(text: str, clip: dict[str, Any]) -> str:
    if not text or not is_china_related_clip(clip):
        return text
    value = text
    for pattern, replacement in CHINA_NEGATIVE_FRAMING_REPLACEMENTS:
        value = pattern.sub(replacement, value)
    return value


def subtitle_sample(clip: dict[str, Any], max_subtitles: int) -> list[dict[str, Any]]:
    sample: list[dict[str, Any]] = []
    for fallback_index, subtitle in enumerate(clip.get("subtitles", [])[:max_subtitles], start=1):
        zh = clean_text(str(subtitle.get("zh", "")))
        en = clean_text(str(subtitle.get("en", "")))
        if zh or en:
            try:
                subtitle_index = int(subtitle.get("index", fallback_index))
            except (TypeError, ValueError):
                subtitle_index = fallback_index
            item: dict[str, Any] = {
                "index": subtitle_index,
                "zh": zh[:180],
                "en": en[:240],
            }
            for key in ("relative_start", "relative_end"):
                if key in subtitle:
                    try:
                        item[key] = round(float(subtitle[key]), 1)
                    except (TypeError, ValueError):
                        pass
            sample.append(item)
    return sample


def all_clip_briefs(plan: dict[str, Any], clips: list[dict[str, Any]], max_subtitles: int) -> list[dict[str, Any]]:
    return [
        clip_brief(plan, clip, index, max_subtitles)
        for index, clip in enumerate(clips, start=1)
    ]


def clip_brief(
    plan: dict[str, Any],
    clip: dict[str, Any],
    index: int,
    max_subtitles: int,
) -> dict[str, Any]:
    return {
        "index": index,
        "duration_seconds": clip_duration(clip),
        "speaker": clean_text(str(clip.get("speaker") or plan.get("speaker") or "")),
        "speaker_context": clean_text(str(clip.get("speaker_context") or plan.get("speaker_context") or "")),
        "source_title": clean_text(str(clip.get("source_title") or plan.get("source_title") or "")),
        "show_date": clean_text(str(plan.get("show_date", ""))),
        "current_title": clean_text(str(clip.get("title", ""))),
        "current_title_lines": [clean_text(str(item)) for item in clip.get("title_lines", []) if item],
        "current_comment": clean_text(str(clip.get("comment", ""))),
        "subtitles": subtitle_sample(clip, max_subtitles),
    }


def has_ascii_letter(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", text))


def entity_candidates_from_text(text: str) -> list[str]:
    value = clean_text(text)
    if not value or not has_ascii_letter(value):
        return []

    patterns = [
        r"\b(?:[A-Z][a-z]+|[A-Z]{2,})(?:\s+(?:[A-Z][a-z]+|[A-Z]{2,}|&|and|of|the|[A-Z][a-z]+\.?)){0,5}",
        r"\b[A-Z]{2,}(?:\s+[A-Z]{2,}){0,4}\b",
    ]
    candidates: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, value):
            candidate = clean_text(match.group(0).strip(" -:|,.;()[]"))
            if len(candidate) < 2:
                continue
            words = [word for word in re.split(r"\s+", candidate) if word]
            if not words:
                continue
            if all(word in GENERIC_ENTITY_STOPWORDS for word in words):
                continue
            if any(word in ENTITY_CANDIDATE_REJECT_WORDS for word in words):
                continue
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def add_query(queries: list[str], seen: set[str], query: str, max_queries: int) -> None:
    query = clean_text(query)
    if not query or query in seen or len(queries) >= max_queries:
        return
    seen.add(query)
    queries.append(query)


def public_lookup_queries(briefs: list[dict[str, Any]], max_queries: int) -> list[str]:
    queries: list[str] = []
    seen: set[str] = set()

    for brief in briefs:
        speaker = clean_text(str(brief.get("speaker", "")))
        context = clean_text(str(brief.get("speaker_context", "")))

        if has_ascii_letter(speaker) and has_ascii_letter(context):
            add_query(queries, seen, f"{speaker} {context} Chinese name official", max_queries)
        if has_ascii_letter(speaker):
            add_query(queries, seen, f"{speaker} Chinese name finance", max_queries)
        if len(queries) >= max_queries:
            return queries

    for brief in briefs:
        speaker = clean_text(str(brief.get("speaker", "")))
        context = clean_text(str(brief.get("speaker_context", "")))
        source_title = clean_text(str(brief.get("source_title", "")))
        source_fields = [speaker, context, source_title]

        for candidate in entity_candidates_from_text(" ".join(source_fields)):
            add_query(queries, seen, f"{candidate} 中文名 官方", max_queries)
            if len(queries) >= max_queries:
                return queries

        if has_ascii_letter(source_title):
            title_query = re.sub(
                r"\b(?:Bloomberg|The China Show|Top Videos?|KC桌面|Video)\b",
                " ",
                source_title,
                flags=re.IGNORECASE,
            )
            title_query = clean_text(title_query)
            if title_query:
                add_query(queries, seen, f"{title_query[:160]} 中文 名称", max_queries)

        if len(queries) >= max_queries:
            break
    return queries


def html_to_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return clean_text(html_lib.unescape(value))


def parse_public_search_results(page: str, max_results: int) -> list[dict[str, str]]:
    pattern = re.compile(
        r"<a[^>]+class=['\"][^'\"]*result-link[^'\"]*['\"][^>]*>(?P<title>.*?)</a>"
        r".*?<td[^>]+class=['\"][^'\"]*result-snippet[^'\"]*['\"][^>]*>(?P<snippet>.*?)</td>"
        r".*?<span[^>]+class=['\"][^'\"]*link-text[^'\"]*['\"][^>]*>(?P<url>.*?)</span>",
        re.IGNORECASE | re.DOTALL,
    )
    results: list[dict[str, str]] = []
    for match in pattern.finditer(page):
        title = html_to_text(match.group("title"))
        snippet = html_to_text(match.group("snippet"))
        url = html_to_text(match.group("url"))
        if not title and not snippet:
            continue
        results.append({
            "title": title[:160],
            "snippet": snippet[:420],
            "url": url[:240],
        })
        if len(results) >= max_results:
            break
    return results


def search_public_web(query: str, max_results: int) -> list[dict[str, str]]:
    url = PUBLIC_SEARCH_URL + "?" + urlencode({"q": query})
    req = Request(url, headers={"User-Agent": PUBLIC_SEARCH_USER_AGENT})
    with urlopen(req, timeout=20) as resp:
        page = resp.read(700_000).decode("utf-8", "replace")
    return parse_public_search_results(page, max_results)


def public_entity_lookup(
    briefs: list[dict[str, Any]],
    *,
    max_queries: int,
    results_per_query: int,
) -> list[dict[str, Any]]:
    lookups: list[dict[str, Any]] = []
    if max_queries < 1 or results_per_query < 1:
        return lookups

    for query in public_lookup_queries(briefs, max_queries):
        try:
            results = search_public_web(query, results_per_query)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            print(f"Public lookup failed for {query!r}: {exc}", flush=True)
            continue
        if not results:
            print(f"Public lookup returned no results for {query!r}", flush=True)
            continue
        print(f"Public lookup: {query!r} -> {len(results)} result(s)", flush=True)
        lookups.append({
            "query": query,
            "results": results,
        })
    return lookups


def entity_guide_system_prompt() -> str:
    return (
        "You are a bilingual financial-news fact checker. Return strict JSON only. "
        "Your job is to identify people, institutions, companies, places, and events "
        "from source metadata and public search snippets, then provide established "
        "Simplified Chinese names. Do not invent translations."
    )


def entity_guide_user_prompt(briefs: list[dict[str, Any]], public_lookup: list[dict[str, Any]]) -> str:
    return f"""Build a Chinese entity translation guide for these finance/news clips.

Source clip metadata:
{json.dumps(briefs, ensure_ascii=False, indent=2)}

Public lookup snippets:
{json.dumps(public_lookup, ensure_ascii=False, indent=2)}

Return JSON:
{{
  "entities": [
    {{
      "source_text": "English or original entity",
      "entity_type": "person|organization|company|place|event|other",
      "preferred_en": "canonical English name if available",
      "preferred_zh": "established Simplified Chinese name",
      "aliases": ["wrong or alternate Chinese/English mentions to replace"],
      "confidence": "high|medium|low",
      "evidence": "short reason from public snippets"
    }}
  ],
  "notes": ["optional notes"]
}}

Rules:
- This is generic. Do not rely on a fixed list of names.
- Prefer official company pages, major financial media, Wikipedia/Wikidata-style summaries, and exact bilingual snippets.
- If public snippets show Traditional Chinese, convert to Simplified Chinese in preferred_zh.
- If there is not enough evidence for a Chinese name, set confidence to low and leave preferred_zh empty or use the original English name.
- Include likely mistranslations from current titles in aliases only when the public evidence supports a better name.
- Keep aliases short; they are used for string replacement.
"""


def parse_entity_guide(result: dict[str, Any]) -> dict[str, Any]:
    raw_entities = result.get("entities")
    if not isinstance(raw_entities, list):
        raw_entities = []

    entities: list[dict[str, Any]] = []
    for raw in raw_entities:
        if not isinstance(raw, dict):
            continue
        preferred_zh = compact_text(str(raw.get("preferred_zh", "")), 24)
        preferred_en = clean_text(str(raw.get("preferred_en", "")))[:80]
        source_text = clean_text(str(raw.get("source_text", "")))[:80]
        if not preferred_zh and not preferred_en:
            continue
        aliases = raw.get("aliases", [])
        if not isinstance(aliases, list):
            aliases = []
        clean_aliases: list[str] = []
        for alias in aliases:
            value = clean_text(to_simplified_common(str(alias)))
            if value and value not in clean_aliases and value != preferred_zh:
                clean_aliases.append(value[:40])
        entities.append({
            "source_text": source_text,
            "entity_type": clean_text(str(raw.get("entity_type", "other")))[:24] or "other",
            "preferred_en": preferred_en,
            "preferred_zh": preferred_zh,
            "aliases": clean_aliases[:8],
            "confidence": clean_text(str(raw.get("confidence", "low")))[:12] or "low",
            "evidence": clean_text(str(raw.get("evidence", "")))[:260],
        })

    notes = result.get("notes")
    if not isinstance(notes, list):
        notes = []
    return {
        "entities": entities,
        "notes": [clean_text(str(note))[:180] for note in notes if clean_text(str(note))][:8],
    }


def build_entity_translation_guide(
    api_key: str,
    briefs: list[dict[str, Any]],
    public_lookup: list[dict[str, Any]],
) -> dict[str, Any]:
    if not public_lookup:
        return {"entities": [], "notes": ["No public lookup snippets were available."]}
    print("Building entity translation guide with DeepSeek", flush=True)
    result = ask_deepseek(
        api_key,
        entity_guide_system_prompt(),
        entity_guide_user_prompt(briefs, public_lookup),
        temperature=0.1,
    )
    return parse_entity_guide(result)


def entity_replacement_pairs(entity_guide: dict[str, Any]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for entity in entity_guide.get("entities", []):
        if not isinstance(entity, dict):
            continue
        preferred = clean_text(str(entity.get("preferred_zh", "")))
        if not preferred:
            continue
        aliases = entity.get("aliases", [])
        if not isinstance(aliases, list):
            aliases = []
        for alias in aliases:
            alias_text = clean_text(str(alias))
            if alias_text and alias_text != preferred:
                pairs.append((alias_text, preferred))
    pairs.sort(key=lambda item: len(item[0]), reverse=True)
    return pairs


def apply_entity_replacements(text: str, entity_guide: dict[str, Any]) -> str:
    value = to_simplified_common(text)
    for alias, preferred in entity_replacement_pairs(entity_guide):
        value = value.replace(alias, preferred)
    return value


def system_prompt() -> str:
    return (
        "You are the chief Chinese title editor for finance/news short videos. "
        "Return strict JSON only. Rewrite titles to improve thumb-stop rate and completion rate "
        "for a China audience while staying factual. Think in cover-copy blocks, not article headlines. "
        "Use credible hooks: big-name institutions, "
        "recognizable people, contrarian tension, surprising consequence, concrete catalyst, "
        "numbers, and curiosity gaps. Apply China-related brand safety: do not frame China, "
        "Chinese markets, Chinese companies, or Chinese policy as fundamentally bad or hopeless. "
        "Never invent facts or names. Treat public lookup snippets "
        "as the source of truth for person and institution names."
    )


def user_prompt(
    briefs: list[dict[str, Any]],
    style: str,
    public_lookup: list[dict[str, Any]],
    entity_guide: dict[str, Any],
) -> str:
    return f"""Refine these clip titles for Chinese vertical short videos.

Input clips:
{json.dumps(briefs, ensure_ascii=False, indent=2)}

Public entity lookup snippets:
{json.dumps(public_lookup, ensure_ascii=False, indent=2) if public_lookup else "[]"}

Entity translation guide:
{json.dumps(entity_guide, ensure_ascii=False, indent=2)}

Return JSON:
{{
  "clips": [
    {{
      "index": 1,
      "title": "完整中文标题",
      "title_lines": ["第一行", "第二行", "第三行"],
      "title_highlights": ["关键词1", "关键词2"],
      "comment": "KC评论：一句话解释为什么值得关注",
      "comment_highlights": ["关键词1"],
      "subtitle_comments": [
        {{
          "subtitle_index": 1,
          "comment": "KC评论：基于这句字幕的短评",
          "comment_highlights": ["关键词1"]
        }}
      ]
    }}
  ]
}}

Title strategy:
- Style: {style}
- Prefer institution/person authority when present: 高盛、摩根士丹利、美联储、特朗普、马斯克、洪灏、邢自强、辜朝明等。
- Use compact authority labels when factual: "高盛王逸", "野村辜朝明", "中金Kevin". If no established Chinese name exists, keep the recognizable English name instead of forced transliteration.
- Prefer counter-intuitive hooks when the transcript supports them, e.g. weak consensus vs unexpected turn, low valuation vs value trap, good data vs market selloff, central bank choice vs being forced.
- Make the viewer ask "why?" or "really?" without cheap exaggeration. A good third line is often a sharp question or pressure point.
- Strong three-line reference formats. Use their structure, not their facts unless supported:
  1. ["高盛王逸", "4万亿城市更新", "会托住楼市吗"] with highlights ["4万亿"]
  2. ["解读腾讯", "AI路径", "关键分叉在哪"] with highlights ["腾讯", "AI"]
  3. ["野村辜朝明", "日本央行", "为何被逼到墙角"] with highlights ["辜朝明", "被逼到墙角"]
  4. ["中金Kevin", "低估值可能是价值陷阱", "等待政策转向消费"] with highlights ["低估值", "价值陷阱", "政策转向消费"]
- More strong patterns: "高盛：楼市真触底了？", "美联储这句话不寻常", "马斯克押错了吗？", "摩根士丹利：最坏时刻过去？"
- Weak patterns to avoid: flat summaries, "今日热点", "核心观点速览", "值得关注", "震惊", "必看", "速看", clickbait with no factual basis.
- Avoid newspaper-style verbs when a short noun phrase works: prefer "腾讯AI路径" over "腾讯正在探索AI业务路径"; prefer "日本央行" over "关于日本央行的讨论".
- China framing rule: if the clip involves China, Chinese companies, Chinese assets, Chinese consumers, Chinese policy, or Chinese macro conditions, do not write titles that sound like China-bashing, national decline, collapse, ridicule, or hopelessness.
- It is OK, and often preferable, to frame China-related clips through constructive or positive angles: policy space, confidence repair, consumption pivot, valuation repair, industrial upgrade, resilience, opportunity, or "can X support Y?".
- If the transcript contains real pressure or risk about China, keep it factual but make the target the market mechanism or policy signal, not China itself. Prefer "楼市承压，政策如何托底？" over "中国楼市完了？".
- Also write a concise KC commentary line that adds context or explains why the clip matters, like a restrained danmaku below subtitles.

Rules:
- Return one refined item for every input index, with the same index.
- First use the entity translation guide to verify person names and institution names. Use preferred_zh when confidence is high or medium; do not literally translate names.
- Convert Traditional Chinese names in public snippets to Simplified Chinese for the final title, e.g. 蓮華 -> 莲华, 資產 -> 资产, 洪灝 -> 洪灏.
- If public snippets conflict, prefer official company pages, major financial media, Wikipedia/Wikidata-style summaries, and exact bilingual mentions.
- title should be short and sharp, ideally 10-24 Chinese characters.
- title_lines must contain exactly 3 non-empty short display lines:
  line 1 = the strongest actor / institution / person, ideally 3-8 Chinese characters or an institution+person label,
  line 2 = the concrete topic, number, asset, policy, or event, ideally 4-10 Chinese characters,
  line 3 = the hook, tension, question, pressure point, or consequence, ideally 5-12 Chinese characters.
- Keep each title line as a punchy block, not a full sentence. No filler like "关于", "表示", "认为", "指出" unless needed for facts.
- title_highlights must be exact substrings from the joined title_lines. Prefer 2-6 character visual anchors: numbers, institution/person names, "低估值", "价值陷阱", "政策转向", "被逼到墙角".
- For China-related titles, highlights should not visually amplify derogatory or doom phrases. Highlight constructive anchors such as "政策转向", "消费", "产业升级", "托底", "修复", "低估值", "4万亿".
- comment must start with "KC评论：" and be one sharp sentence, ideally 16-34 Chinese characters after the prefix.
- comment should add an editorial lens: why this matters, what tension it reveals, or what signal to watch next.
- comment_highlights must be exact substrings of comment. Prefer the entity/event/risk keyword, not the prefix.
- Do not repeat the title verbatim in comment.
- subtitle_comments must contain one item for every input subtitle in that clip's subtitles array.
- Each subtitle_comments[].subtitle_index must exactly match the input subtitle index.
- Each subtitle comment must be dynamic and grounded in that specific subtitle's zh/en text, not a generic clip-level slogan.
- Subtitle comments should be shorter than the clip-level comment: ideally 10-24 Chinese characters after "KC评论：", hard limit 28 Chinese characters.
- Subtitle comments must end as a complete phrase; do not leave trailing fragments after truncation.
- For adjacent subtitles, vary the angle: signal, tension, implication, risk, or why the line matters.
- Do not use source labels such as 彭博独家, 独家, Bloomberg Exclusive.
- Do not use emojis, markdown, quotation marks, hashtags, or numbering.
- Do not use financial-advice wording.
- Avoid sensitive Chinese words: rephrase 投资/股票/A股/港股/美股 when needed.
- Preserve the clip's factual meaning. If the transcript does not support a dramatic claim, use a curiosity question instead of stating it as fact.
"""


def parse_items(result: dict[str, Any]) -> dict[int, dict[str, Any]]:
    raw_items = result.get("clips")
    if not isinstance(raw_items, list):
        raw_items = result.get("items")
    if not isinstance(raw_items, list):
        return {}

    items: dict[int, dict[str, Any]] = {}
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        try:
            index = int(raw["index"])
        except (KeyError, TypeError, ValueError):
            continue
        items[index] = raw
    return items


def fallback_lines(clip: dict[str, Any], entity_guide: dict[str, Any] | None = None) -> list[str]:
    lines = clip.get("title_lines")
    if isinstance(lines, list):
        cleaned = [
            compact_text(
                china_safe_title_text(apply_entity_replacements(str(line), entity_guide or {}), clip),
                TITLE_LINE_LIMITS[min(idx, 2)],
            )
            for idx, line in enumerate(lines[:3])
        ]
        if len(cleaned) == 3 and all(cleaned):
            return cleaned

    speaker = compact_text(
        china_safe_title_text(apply_entity_replacements(str(clip.get("speaker", "")), entity_guide or {}), clip),
        TITLE_LINE_LIMITS[0],
    )
    title = compact_title(
        china_safe_title_text(apply_entity_replacements(str(clip.get("title", "")), entity_guide or {}), clip)
    )
    if "：" in title:
        left, right = title.split("：", 1)
    elif ":" in title:
        left, right = title.split(":", 1)
    else:
        left, right = speaker or "Bloomberg", title
    return [
        compact_text(china_safe_title_text(left or speaker or "Bloomberg", clip), TITLE_LINE_LIMITS[0]),
        compact_text(china_safe_title_text(right or title or "市场焦点", clip), TITLE_LINE_LIMITS[1]),
        "关键转折来了",
    ]


def normalize_comment_payload(
    raw_comment: Any,
    raw_highlights: Any,
    fallback_comment: str,
    entity_guide: dict[str, Any],
    *,
    body_max_chars: int,
) -> tuple[str, list[str]]:
    comment = apply_entity_replacements(str(raw_comment or ""), entity_guide)
    comment = safe_zh(to_simplified_common(strip_title_badges(comment)))
    comment = re.sub(r"\s+", "", comment)
    comment = comment.strip(" ，,。；;、｜|-")
    if comment and not comment.startswith("KC评论："):
        comment = "KC评论：" + comment.removeprefix("KC评论:").removeprefix("KC点评：").removeprefix("点评：")
    if not comment:
        comment = fallback_comment
    if comment and not comment.startswith("KC评论："):
        comment = "KC评论：" + comment.removeprefix("KC评论:").removeprefix("KC点评：").removeprefix("点评：")

    prefix = "KC评论："
    if len(comment) > len(prefix) + body_max_chars:
        body = comment[len(prefix):] if comment.startswith(prefix) else comment
        comment = prefix + truncate_comment_body(body, body_max_chars)

    if isinstance(raw_highlights, list):
        raw_highlights = [apply_entity_replacements(str(item), entity_guide) for item in raw_highlights]
    highlights = normalize_highlights(raw_highlights, comment, limit=2)
    if not highlights:
        body = comment.removeprefix("KC评论：")
        if body:
            highlights = [body[: min(8, len(body))]]
    return comment, highlights[:2]


def truncate_comment_body(body: str, max_chars: int) -> str:
    body = body.strip(" ，,。；;、｜|-")
    if len(body) <= max_chars:
        return body
    clipped = body[:max_chars].rstrip(" ，,。；;、｜|-")
    min_keep = max(8, max_chars // 2)
    for marker in ("，", "；", "。", "、", ",", ";"):
        idx = clipped.rfind(marker)
        if idx >= min_keep:
            return clipped[:idx].rstrip(" ，,。；;、｜|-")
    return clipped


def normalize_comment(raw: dict[str, Any], clip: dict[str, Any], entity_guide: dict[str, Any]) -> tuple[str, list[str]]:
    title = compact_title(str(clip.get("title", "")))
    fallback = f"KC评论：这条线索值得继续跟踪" if not title else f"KC评论：{title[:14]}背后有新信号"
    return normalize_comment_payload(
        raw.get("comment", ""),
        raw.get("comment_highlights"),
        fallback,
        entity_guide,
        body_max_chars=34,
    )


def subtitle_index(subtitle: dict[str, Any], fallback: int) -> int:
    try:
        return int(subtitle.get("index", fallback))
    except (TypeError, ValueError):
        return fallback


def fallback_subtitle_comment(clip: dict[str, Any], subtitle: dict[str, Any]) -> str:
    raw_highlights = subtitle.get("zh_highlights")
    if isinstance(raw_highlights, list):
        for raw in raw_highlights:
            key = compact_text(str(raw), 10)
            if key:
                return f"KC评论：盯住{key}这个信号"

    zh = clean_text(str(subtitle.get("zh_filtered") or subtitle.get("zh") or ""))
    if zh:
        phrase = compact_text(zh, 12)
        if phrase:
            return f"KC评论：{phrase}是关键信号"

    title = compact_title(str(clip.get("title", "")))
    return f"KC评论：{title[:12]}这里有新信号" if title else "KC评论：这句是判断关键"


def normalize_subtitle_comments(
    raw: dict[str, Any],
    clip: dict[str, Any],
    entity_guide: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_items = raw.get("subtitle_comments")
    if not isinstance(raw_items, list):
        return []

    subtitles = clip.get("subtitles", [])
    if not isinstance(subtitles, list) or not subtitles:
        return []

    by_index: dict[int, dict[str, Any]] = {}
    for fallback, subtitle in enumerate(subtitles, start=1):
        if isinstance(subtitle, dict):
            by_index[subtitle_index(subtitle, fallback)] = subtitle

    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("subtitle_index", item.get("index", 0)))
        except (TypeError, ValueError):
            continue
        if idx in seen or idx not in by_index:
            continue
        comment, highlights = normalize_comment_payload(
            item.get("comment", ""),
            item.get("comment_highlights", item.get("highlights")),
            fallback_subtitle_comment(clip, by_index[idx]),
            entity_guide,
            body_max_chars=34,
        )
        normalized.append({
            "subtitle_index": idx,
            "comment": comment,
            "comment_highlights": highlights,
        })
        seen.add(idx)

    normalized.sort(key=lambda item: int(item["subtitle_index"]))
    return normalized


def normalize_item(raw: dict[str, Any], clip: dict[str, Any], entity_guide: dict[str, Any]) -> dict[str, Any] | None:
    fallback = fallback_lines(clip, entity_guide)
    raw_lines = raw.get("title_lines")
    if not isinstance(raw_lines, list) or len(raw_lines) != 3:
        raw_lines = fallback

    lines = [
        compact_text(
            china_safe_title_text(apply_entity_replacements(str(raw_lines[idx]), entity_guide), clip),
            TITLE_LINE_LIMITS[idx],
        ) or fallback[idx]
        for idx in range(3)
    ]
    if not all(lines):
        return None

    title = compact_title(china_safe_title_text(apply_entity_replacements(str(raw.get("title", "")), entity_guide), clip))
    if not title:
        title = f"{lines[0]}：{lines[1]}，{lines[2]}"

    joined = "".join(lines)
    raw_highlights = raw.get("title_highlights")
    if isinstance(raw_highlights, list):
        raw_highlights = [apply_entity_replacements(str(item), entity_guide) for item in raw_highlights]
    highlights = normalize_highlights(raw_highlights, joined, limit=3)
    if not highlights:
        highlights = [line for line in lines[1:] if line][:2]
    comment, comment_highlights = normalize_comment(raw, clip, entity_guide)

    return {
        "title": title,
        "title_lines": lines,
        "title_highlights": highlights[:3],
        "comment": comment,
        "comment_highlights": comment_highlights,
        "subtitle_comments": normalize_subtitle_comments(raw, clip, entity_guide),
    }


def refine_batch(
    api_key: str,
    plan: dict[str, Any],
    clips: list[dict[str, Any]],
    indexes: list[int],
    *,
    style: str,
    max_subtitles: int,
    public_lookup: list[dict[str, Any]],
    entity_guide: dict[str, Any],
) -> dict[int, dict[str, Any]]:
    briefs = [
        clip_brief(plan, clips[index - 1], index, max_subtitles)
        for index in indexes
    ]
    result = ask_deepseek(
        api_key,
        system_prompt(),
        user_prompt(briefs, style, public_lookup, entity_guide),
        temperature=0.45,
    )
    parsed = parse_items(result)

    normalized: dict[int, dict[str, Any]] = {}
    for index in indexes:
        item = parsed.get(index)
        if not item:
            continue
        refined = normalize_item(item, clips[index - 1], entity_guide)
        if refined:
            normalized[index] = refined
    return normalized


def refine_titles(
    plan: dict[str, Any],
    *,
    api_key: str,
    style: str,
    batch_size: int,
    max_subtitles: int,
    public_lookup: list[dict[str, Any]],
    entity_guide: dict[str, Any],
) -> dict[int, dict[str, Any]]:
    clips = plan.get("clips", [])
    if not isinstance(clips, list) or not clips:
        raise SystemExit("No clips found in plan")

    refinements: dict[int, dict[str, Any]] = {}
    indexes = list(range(1, len(clips) + 1))
    for offset in range(0, len(indexes), batch_size):
        batch_indexes = indexes[offset:offset + batch_size]
        print(
            "Refining titles "
            + ",".join(str(index) for index in batch_indexes)
            + " with DeepSeek",
            flush=True,
        )
        refinements.update(
            refine_batch(
                api_key,
                plan,
                clips,
                batch_indexes,
                style=style,
                max_subtitles=max_subtitles,
                public_lookup=public_lookup,
                entity_guide=entity_guide,
            )
        )

    missing = [index for index in indexes if index not in refinements]
    for index in missing:
        print(f"Retrying missing title refinement for clip {index}", flush=True)
        refinements.update(
            refine_batch(
                api_key,
                plan,
                clips,
                [index],
                style=style,
                max_subtitles=max_subtitles,
                public_lookup=public_lookup,
                entity_guide=entity_guide,
            )
        )

    missing = [index for index in indexes if index not in refinements]
    if missing:
        raise SystemExit("DeepSeek did not return valid title refinements for clips: " + ",".join(map(str, missing)))

    return refinements


def apply_refinements(
    plan: dict[str, Any],
    refinements: dict[int, dict[str, Any]],
    style: str,
    public_lookup: list[dict[str, Any]],
    entity_guide: dict[str, Any],
) -> None:
    clips = plan["clips"]
    for index, refined in refinements.items():
        clip = clips[index - 1]
        clip.setdefault("original_title", clip.get("title", ""))
        clip.setdefault("original_title_lines", clip.get("title_lines", []))
        clip.setdefault("original_title_highlights", clip.get("title_highlights", []))
        clip.setdefault("original_comment", clip.get("comment", ""))
        clip.setdefault("original_subtitle_comments", clip.get("subtitle_comments", []))
        clip["title"] = refined["title"]
        clip["title_lines"] = refined["title_lines"]
        clip["title_highlights"] = refined["title_highlights"]
        clip["comment"] = refined["comment"]
        clip["comment_highlights"] = refined["comment_highlights"]
        clip["subtitle_comments"] = refined["subtitle_comments"]
        clip["title_refined"] = True
        clip["title_refine_style"] = style

    plan["title_refine"] = {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "style": style,
        "clip_count": len(refinements),
        "entity_translation_guide": entity_guide,
        "public_lookup": public_lookup,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True, help="highlight_plan.json to rewrite in place.")
    parser.add_argument("--style", default="factual clickbait for China finance audience")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-subtitles", type=int, default=DEFAULT_MAX_SUBTITLES)
    parser.add_argument("--lookup-max-queries", type=int, default=DEFAULT_LOOKUP_MAX_QUERIES)
    parser.add_argument("--lookup-results-per-query", type=int, default=DEFAULT_LOOKUP_RESULTS_PER_QUERY)
    parser.add_argument("--no-public-lookup", action="store_true")
    args = parser.parse_args()

    if args.batch_size < 1:
        raise SystemExit("--batch-size must be at least 1")
    if args.max_subtitles < 1:
        raise SystemExit("--max-subtitles must be at least 1")

    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("DEEPSEEK_API_KEY is required")

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    clips = plan.get("clips", [])
    if not isinstance(clips, list) or not clips:
        raise SystemExit("No clips found in plan")

    briefs = all_clip_briefs(plan, clips, args.max_subtitles)
    lookup: list[dict[str, Any]] = []
    if not args.no_public_lookup:
        lookup = public_entity_lookup(
            briefs,
            max_queries=args.lookup_max_queries,
            results_per_query=args.lookup_results_per_query,
        )
        if not lookup:
            print("No public entity lookup snippets collected; continuing with transcript context only.", flush=True)

    entity_guide = build_entity_translation_guide(api_key, briefs, lookup)

    refinements = refine_titles(
        plan,
        api_key=api_key,
        style=args.style,
        batch_size=args.batch_size,
        max_subtitles=args.max_subtitles,
        public_lookup=lookup,
        entity_guide=entity_guide,
    )
    apply_refinements(plan, refinements, args.style, lookup, entity_guide)
    args.plan.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for index in sorted(refinements):
        refined = refinements[index]
        print(
            f"{index:02d}. {refined['title']} / {' | '.join(refined['title_lines'])} / {refined['comment']}",
            flush=True,
        )
    print(f"Wrote refined titles: {args.plan}", flush=True)


if __name__ == "__main__":
    main()
