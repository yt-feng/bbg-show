#!/usr/bin/env python3
"""Shared Chinese wording guard for KC Desktop finance/news clips."""

from __future__ import annotations

import re
from typing import Any


CHINA_CONTEXT_RE = re.compile(
    r"(中国|中资|中企|中概|内地|国内|人民币|楼市|房价|房地产|地产|A股|港股|"
    r"China|Chinese|Hong Kong|renminbi|yuan|property|housing|real estate)",
    re.IGNORECASE,
)

WORDING_GUARD_PROMPT = """Wording guard for all Chinese output:
- Do not use hard crisis/doom financial wording such as 经济危机、金融危机、债务危机、流动性危机、危机、崩盘、崩溃、完了、没救、惨了、萧条、衰退.
- Prefer neutral market-language alternatives: 流动性变化、信贷变化、债务压力、政策信号、需求变化、信心修复、估值重估、周期压力、结构调整、市场波动、边际变化.
- For China-related clips, do not frame China, Chinese companies, Chinese assets, Chinese consumers, or Chinese policy as hopeless, collapsing, being mocked, or fundamentally bad.
- If the source is negative about China-related macro/markets, keep the fact but soften the Chinese wording: talk about pressure, policy response, demand repair, liquidity change, confidence repair, valuation reset, or structural adjustment.
- Titles must be sharp but not doom-heavy. Never put 经济危机/金融危机/债务危机/危机 in title, title_lines, title_highlights, comment, or subtitle_comments.
- Title fields must not use financial-advice or product-sale wording such as 资产管理、投资、股票、基金、理财、保险、投顾、荐股、买入、卖出. Rephrase with neutral market wording such as 资管、配置、权益资产、市场、产品、财富配置、保障、观点."""


GENERAL_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("流动性危机", "流动性变化"),
    ("经济危机", "流动性变化"),
    ("金融危机", "流动性变化"),
    ("信贷危机", "信贷变化"),
    ("债务危机", "债务压力"),
    ("主权债务危机", "主权债务压力"),
    ("银行业危机", "银行体系承压"),
    ("银行危机", "银行体系承压"),
    ("房地产危机", "楼市调整"),
    ("楼市危机", "楼市调整"),
    ("房价危机", "房价调整"),
    ("货币危机", "汇率波动"),
    ("汇率危机", "汇率波动"),
    ("就业危机", "就业压力"),
    ("失业危机", "就业压力"),
    ("失业潮", "就业压力"),
    ("裁员潮", "人员调整"),
    ("经济崩盘", "经济承压"),
    ("市场崩盘", "市场大幅波动"),
    ("股市崩盘", "市场大幅波动"),
    ("楼市崩盘", "楼市承压"),
    ("房价崩盘", "房价承压"),
    ("崩盘", "大幅波动"),
    ("崩溃", "承压"),
    ("塌了", "承压"),
    ("垮了", "承压"),
    ("完了", "承压"),
    ("没救了", "承压"),
    ("没救", "承压"),
    ("药丸", "承压"),
    ("太惨", "压力加大"),
    ("惨了", "压力加大"),
    ("很惨", "压力加大"),
    ("惨败", "遇挑战"),
    ("输麻了", "遇挑战"),
    ("萧条", "低位调整"),
    ("大萧条", "深度调整"),
    ("衰退", "放缓"),
    ("急剧恶化", "明显承压"),
    ("恶化", "承压"),
    ("爆雷", "压力暴露"),
    ("暴雷", "压力暴露"),
    ("泡沫破裂", "估值重估"),
    ("系统性风险", "系统性压力"),
    ("黑天鹅", "意外变量"),
)

TITLE_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("危机", "变化"),
    ("灾难", "压力"),
    ("噩梦", "压力"),
    ("恐慌", "波动"),
)

TITLE_SENSITIVE_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("资产管理公司", "资管机构"),
    ("資產管理公司", "资管机构"),
    ("资产管理", "资管"),
    ("資產管理", "资管"),
    ("投资银行", "投行"),
    ("投資銀行", "投行"),
    ("投资管理", "配置管理"),
    ("投資管理", "配置管理"),
    ("投资建议", "观点参考"),
    ("投資建議", "观点参考"),
    ("投资评级", "机构评级"),
    ("投資評級", "机构评级"),
    ("买入评级", "正面评级"),
    ("買入評級", "正面评级"),
    ("卖出评级", "负面评级"),
    ("賣出評級", "负面评级"),
    ("投资级债", "高评级债"),
    ("投資級債", "高评级债"),
    ("投资级", "高评级"),
    ("投資級", "高评级"),
    ("投资者", "市场参与者"),
    ("投資者", "市场参与者"),
    ("投资人", "资金方"),
    ("投資人", "资金方"),
    ("投资组合", "组合"),
    ("投資組合", "组合"),
    ("投资主题", "主线"),
    ("投資主題", "主线"),
    ("投资逻辑", "配置逻辑"),
    ("投資邏輯", "配置逻辑"),
    ("投资机会", "机会线索"),
    ("投資機會", "机会线索"),
    ("投资策略", "配置思路"),
    ("投資策略", "配置思路"),
    ("投资", "配置"),
    ("投資", "配置"),
    ("A股", "内地市场"),
    ("Ａ股", "内地市场"),
    ("港股", "香港市场"),
    ("美股", "美国市场"),
    ("中国股票", "中国市场"),
    ("中國股票", "中国市场"),
    ("股票", "权益资产"),
    ("股市", "市场"),
    ("个股", "单家公司"),
    ("基金经理", "组合经理"),
    ("基金經理", "组合经理"),
    ("基金", "产品"),
    ("理财", "财富配置"),
    ("理財", "财富配置"),
    ("保险", "保障"),
    ("保險", "保障"),
    ("投顾", "顾问"),
    ("投顧", "顾问"),
    ("荐股", "观点"),
    ("薦股", "观点"),
    ("买入", "看多"),
    ("買入", "看多"),
    ("卖出", "看淡"),
    ("賣出", "看淡"),
)

CHINA_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:外资|资金|资本)(?:正|正在)?(?:集体)?逃离(中国资产|中国市场|A股|港股|中概股?)"), r"\1再定价"),
    (re.compile(r"(?:外资逃离|资金逃离|资本外逃|集体逃离)"), "资金再配置"),
    (re.compile(r"(?:唱衰|看空)(中国(?:资产|市场|经济|楼市|房地产)?)"), r"\1信心修复"),
    (re.compile(r"中国(?:资产|市场|经济)?(?:不行了?|完了|没救了?)"), "中国相关资产承压"),
    (re.compile(r"(中国(?:楼市|房地产|房价)?)(?:崩盘|崩溃|塌了|垮了)"), r"\1承压"),
    (re.compile(r"(人民币|A股|港股|中概股?)(?:崩盘|崩溃|塌了|垮了)"), r"\1波动加大"),
    (re.compile(r"(?:被抛弃|被放弃|遭抛弃)"), "被重新定价"),
    (re.compile(r"(?:中国式危机|中国危机)"), "中国市场变化"),
    (re.compile(r"危机"), "压力"),
)


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def is_china_context(text: str) -> bool:
    return bool(CHINA_CONTEXT_RE.search(text))


def sanitize_zh_wording(text: str, *, context: str = "", for_title: bool = False) -> str:
    value = clean_text(text)
    if not value:
        return value

    for old, new in GENERAL_REPLACEMENTS:
        value = value.replace(old, new)

    if for_title:
        for old, new in TITLE_SENSITIVE_REPLACEMENTS:
            value = value.replace(old, new)
        for old, new in TITLE_REPLACEMENTS:
            value = value.replace(old, new)

    combined = f"{value} {context}"
    if is_china_context(combined):
        for pattern, replacement in CHINA_REPLACEMENTS:
            value = pattern.sub(replacement, value)

    # Clean up awkward repeats after replacement.
    value = value.replace("承压承压", "承压")
    value = value.replace("变化变化", "变化")
    value = value.replace("配置配置", "配置")
    value = value.replace("资管管理", "资管")
    value = re.sub(r"(流动性变化)(?:变化|压力)", r"\1", value)
    return clean_text(value)


def _stringify_list(values: Any) -> str:
    if not isinstance(values, list):
        return ""
    return " ".join(str(item) for item in values if item is not None)


def _clip_context(plan: dict[str, Any], clip: dict[str, Any]) -> str:
    pieces = [
        str(plan.get("speaker", "")),
        str(plan.get("speaker_context", "")),
        str(plan.get("source_title", "")),
        str(clip.get("speaker", "")),
        str(clip.get("speaker_context", "")),
        str(clip.get("source_title", "")),
        str(clip.get("title", "")),
        _stringify_list(clip.get("title_lines")),
    ]
    for subtitle in clip.get("subtitles", []):
        if isinstance(subtitle, dict):
            pieces.append(str(subtitle.get("zh", "")))
            pieces.append(str(subtitle.get("en", "")))
    return " ".join(pieces)


def _sanitize_list(values: Any, *, context: str, for_title: bool) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for item in values:
        text = sanitize_zh_wording(str(item), context=context, for_title=for_title)
        if text:
            result.append(text)
    return result


def sanitize_clip_wording(plan: dict[str, Any], clip: dict[str, Any]) -> None:
    context = _clip_context(plan, clip)

    for key in ("title", "comment"):
        if key in clip:
            clip[key] = sanitize_zh_wording(str(clip.get(key, "")), context=context, for_title=(key == "title"))

    if isinstance(clip.get("title_lines"), list):
        clip["title_lines"] = _sanitize_list(clip["title_lines"], context=context, for_title=True)
    if isinstance(clip.get("title_highlights"), list):
        joined = "".join(clip.get("title_lines", [])) or str(clip.get("title", ""))
        title_highlights = [
            item for item in _sanitize_list(clip["title_highlights"], context=context, for_title=True)
            if item and (not joined or item in joined)
        ]
        if not title_highlights and isinstance(clip.get("title_lines"), list):
            title_highlights = [line for line in clip["title_lines"][1:3] if line][:2]
        clip["title_highlights"] = title_highlights
    if isinstance(clip.get("comment_highlights"), list):
        clip["comment_highlights"] = _sanitize_list(clip["comment_highlights"], context=context, for_title=False)

    subtitles = clip.get("subtitles")
    if isinstance(subtitles, list):
        for subtitle in subtitles:
            if not isinstance(subtitle, dict):
                continue
            sub_context = f"{context} {subtitle.get('en', '')}"
            for key in ("zh", "zh_filtered"):
                if key in subtitle:
                    subtitle[key] = sanitize_zh_wording(str(subtitle.get(key, "")), context=sub_context)
            if isinstance(subtitle.get("zh_highlights"), list):
                zh = str(subtitle.get("zh_filtered") or subtitle.get("zh") or "")
                subtitle["zh_highlights"] = [
                    item for item in _sanitize_list(subtitle["zh_highlights"], context=sub_context, for_title=False)
                    if item and (not zh or item in zh)
                ]

    subtitle_comments = clip.get("subtitle_comments")
    if isinstance(subtitle_comments, list):
        for item in subtitle_comments:
            if not isinstance(item, dict):
                continue
            if "comment" in item:
                item["comment"] = sanitize_zh_wording(str(item.get("comment", "")), context=context)
            if isinstance(item.get("comment_highlights"), list):
                item["comment_highlights"] = _sanitize_list(item["comment_highlights"], context=context, for_title=False)


def sanitize_plan_wording(plan: dict[str, Any]) -> dict[str, Any]:
    clips = plan.get("clips")
    if isinstance(clips, list):
        for clip in clips:
            if isinstance(clip, dict):
                sanitize_clip_wording(plan, clip)
    return plan
