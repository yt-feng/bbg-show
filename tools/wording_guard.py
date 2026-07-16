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
- Never directly use 房价/房價/住房价格/住宅价格/房屋价格/楼市价格 in Chinese output. Preserve the source direction, number, and unit, but paraphrase by context: use 地产市场上行/调整/趋稳/修复 for direction, 居住成本/住房可负担性/置业负担 for affordability, 地产估值 for valuation, and 住宅成交水平 for absolute levels. Do not evade this rule with another blunt housing-price synonym.
- Titles must be sharp but not doom-heavy. Never put 经济危机/金融危机/债务危机/危机 in title, title_lines, title_highlights, comment, or subtitle_comments.
- Title fields must not use financial-advice or product-sale wording such as 资产管理、投资、股票、基金、理财、保险、投顾、荐股、买入、卖出. Rephrase with neutral market wording such as 资管、配置、权益资产、市场、产品、财富配置、保障、观点."""


HOUSING_PRICE_ALIASES: tuple[tuple[str, str], ...] = (
    ("新建商品住宅销售价格", "新房价"),
    ("新建商品住宅销售價格", "新房价"),
    ("新建商品房销售价格", "新房价"),
    ("新建商品房销售價格", "新房价"),
    ("新建住宅销售价格", "新房价"),
    ("新建住宅销售價格", "新房价"),
    ("二手住宅销售价格", "二手房价"),
    ("二手住宅销售價格", "二手房价"),
    ("二手房销售价格", "二手房价"),
    ("二手房销售價格", "二手房价"),
    ("商品住宅销售价格", "房价"),
    ("商品住宅销售價格", "房价"),
    ("商品房销售价格", "房价"),
    ("商品房销售價格", "房价"),
    ("住宅销售价格", "房价"),
    ("住宅销售價格", "房价"),
    ("新房均价", "新房价均价"),
    ("新房均價", "新房价均价"),
    ("二手房均价", "二手房价均价"),
    ("二手房均價", "二手房价均价"),
    ("住房均价", "房价均价"), ("住房均價", "房价均价"),
    ("住宅均价", "房价均价"), ("住宅均價", "房价均价"),
    ("房屋均价", "房价均价"), ("房屋均價", "房价均价"),
    ("房地产均价", "房价均价"), ("房地產均價", "房价均价"),
    ("房产均价", "房价均价"), ("房產均價", "房价均价"),
    ("商品房均价", "房价均价"), ("商品房均價", "房价均价"),
    ("楼盘均价", "房价均价"), ("樓盤均價", "房价均价"),
    ("物业均价", "房价均价"), ("物業均價", "房价均价"),
    ("楼市均价", "房价均价"), ("樓市均價", "房价均价"),
    ("新房价格", "新房价"),
    ("新房價格", "新房价"),
    ("新房售价", "新房价"),
    ("新房售價", "新房价"),
    ("二手房价格", "二手房价"),
    ("二手房價格", "二手房价"),
    ("二手房售价", "二手房价"),
    ("二手房售價", "二手房价"),
    ("住房价格", "房价"), ("住房價格", "房价"),
    ("住房售价", "房价"), ("住房售價", "房价"),
    ("住宅价格", "房价"), ("住宅價格", "房价"),
    ("住宅售价", "房价"), ("住宅售價", "房价"),
    ("房屋价格", "房价"), ("房屋價格", "房价"),
    ("房屋售价", "房价"), ("房屋售價", "房价"),
    ("房地产价格", "房价"), ("房地產價格", "房价"),
    ("房地产售价", "房价"), ("房地產售價", "房价"),
    ("地产价格", "房价"), ("地產價格", "房价"),
    ("地产售价", "房价"), ("地產售價", "房价"),
    ("房产价格", "房价"), ("房產價格", "房价"),
    ("房产售价", "房价"), ("房產售價", "房价"),
    ("商品房价格", "房价"), ("商品房價格", "房价"),
    ("楼盘价格", "房价"), ("樓盤價格", "房价"),
    ("楼盘售价", "房价"), ("樓盤售價", "房价"),
    ("物业价格", "房价"), ("物業價格", "房价"),
    ("物业售价", "房价"), ("物業售價", "房价"),
    ("楼市价格", "房价"), ("樓市價格", "房价"),
    ("房價", "房价"),
    ("樓價", "房价"),
    ("楼价", "房价"),
)

HOUSING_PRICE_TERM_PATTERN = r"(?P<segment>二手|新)?房价"
HOUSING_CURRENCY_AMOUNT_PATTERN = (
    r"(?:\d[\d,]*(?:\.\d+)?|[一二三四五六七八九十百千万亿两]+)\s*"
    r"(?:(?:万|千|百|亿)?(?:元|美元|港元|人民币|英镑|欧元)|"
    r"(?:万|千|百|亿)(?![%％]))"
    r"(?:\s*(?:[/／]|每)(?:平方米|平米|㎡))?"
)
HOUSING_BASIS_PATTERN = r"(?P<basis>同比|环比)?"
HOUSING_DEGREE_PATTERN = r"(?P<degree>大幅|明显|快速|持续|小幅|温和)?"


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


def _housing_subject(segment: str | None) -> str:
    if segment == "新":
        return "新房市场"
    if segment == "二手":
        return "二手房市场"
    return "地产市场"


def _housing_level(segment: str | None, *, average: bool = False, median: bool = False) -> str:
    prefix = f"{segment}房" if segment else "住宅"
    qualifier = "平均" if average else "中位" if median else ""
    return f"{prefix}{qualifier}成交水平"


def _housing_cost(segment: str | None) -> str:
    if segment == "新":
        return "新房置业成本"
    if segment == "二手":
        return "二手房置业成本"
    return "居住成本"


def _housing_valuation(segment: str | None, *, pressure: bool = False) -> str:
    value = f"{_housing_subject(segment)}估值" if segment else "地产估值"
    return f"{value}压力" if pressure else value


def neutralize_housing_price_wording(text: str) -> str:
    """Paraphrase direct housing-price language without losing facts or units."""
    value = text
    for old, new in HOUSING_PRICE_ALIASES:
        value = value.replace(old, new)

    # Preserve whether the source is discussing all housing, new homes, or
    # second-hand homes while replacing the direct price term.
    value = re.sub(
        rf"平均{HOUSING_PRICE_TERM_PATTERN}",
        lambda match: _housing_level(match.group("segment"), average=True),
        value,
    )
    value = re.sub(
        rf"{HOUSING_PRICE_TERM_PATTERN}(?:均价|平均值)",
        lambda match: _housing_level(match.group("segment"), average=True),
        value,
    )
    value = re.sub(
        rf"{HOUSING_PRICE_TERM_PATTERN}中位数",
        lambda match: _housing_level(match.group("segment"), median=True),
        value,
    )

    value = re.sub(
        rf"{HOUSING_PRICE_TERM_PATTERN}(?:负担能力|可负担性)",
        "住房可负担性",
        value,
    )
    value = re.sub(rf"{HOUSING_PRICE_TERM_PATTERN}收入比", "住房成本收入比", value)
    value = re.sub(rf"{HOUSING_PRICE_TERM_PATTERN}负担", "置业负担", value)
    value = re.sub(
        rf"{HOUSING_PRICE_TERM_PATTERN}泡沫",
        lambda match: _housing_valuation(match.group("segment"), pressure=True),
        value,
    )
    value = re.sub(
        rf"{HOUSING_PRICE_TERM_PATTERN}估值",
        lambda match: _housing_valuation(match.group("segment")),
        value,
    )

    trend_suffixes = {
        "涨幅": "升幅",
        "跌幅": "调整幅度",
        "涨跌": "表现",
        "走势": "表现",
        "趋势": "趋势",
        "预期": "预期",
        "变化": "变化",
        "波动": "波动",
    }
    value = re.sub(
        rf"{HOUSING_PRICE_TERM_PATTERN}(?:{'|'.join(trend_suffixes)})",
        lambda match: _housing_subject(match.group("segment"))
        + trend_suffixes[match.group(0).removeprefix((match.group("segment") or "") + "房价")],
        value,
    )

    # When the change itself is denominated in currency, describe the
    # transaction level being adjusted rather than a market moving by yuan.
    def replace_absolute_change(match: re.Match[str], direction: str) -> str:
        return (
            _housing_level(match.group("segment"))
            + (match.group("basis") or "")
            + (match.group("degree") or "")
            + direction
            + match.group("amount")
        )

    up_words = r"上涨|上升|走高|大涨|飙升|涨"
    down_words = r"下跌|下降|走低|大跌|暴跌|跌"
    value = re.sub(
        rf"{HOUSING_PRICE_TERM_PATTERN}\s*{HOUSING_BASIS_PATTERN}{HOUSING_DEGREE_PATTERN}"
        rf"(?:{up_words})\s*(?P<amount>{HOUSING_CURRENCY_AMOUNT_PATTERN})",
        lambda match: replace_absolute_change(match, "上调"),
        value,
    )
    value = re.sub(
        rf"{HOUSING_PRICE_TERM_PATTERN}\s*{HOUSING_BASIS_PATTERN}{HOUSING_DEGREE_PATTERN}"
        rf"(?:{down_words})\s*(?P<amount>{HOUSING_CURRENCY_AMOUNT_PATTERN})",
        lambda match: replace_absolute_change(match, "下调"),
        value,
    )

    # Explicit from/to currency levels are also absolute transaction levels.
    value = re.sub(
        rf"{HOUSING_PRICE_TERM_PATTERN}\s*从\s*(?P<start>{HOUSING_CURRENCY_AMOUNT_PATTERN})"
        rf"\s*(?:{up_words})到\s*(?P<end>{HOUSING_CURRENCY_AMOUNT_PATTERN})",
        lambda match: (
            f"{_housing_level(match.group('segment'))}从{match.group('start')}"
            f"上调至{match.group('end')}"
        ),
        value,
    )
    value = re.sub(
        rf"{HOUSING_PRICE_TERM_PATTERN}\s*从\s*(?P<start>{HOUSING_CURRENCY_AMOUNT_PATTERN})"
        rf"\s*(?:{down_words})到\s*(?P<end>{HOUSING_CURRENCY_AMOUNT_PATTERN})",
        lambda match: (
            f"{_housing_level(match.group('segment'))}从{match.group('start')}"
            f"下调至{match.group('end')}"
        ),
        value,
    )

    def replace_market_direction(match: re.Match[str], direction: str) -> str:
        return (
            _housing_subject(match.group("segment"))
            + (match.group("basis") or "")
            + (match.group("degree") or "")
            + direction
        )

    value = re.sub(
        rf"{HOUSING_PRICE_TERM_PATTERN}\s*{HOUSING_BASIS_PATTERN}{HOUSING_DEGREE_PATTERN}(?:{up_words})",
        lambda match: replace_market_direction(match, "上行"),
        value,
    )
    value = re.sub(
        rf"{HOUSING_PRICE_TERM_PATTERN}\s*{HOUSING_BASIS_PATTERN}{HOUSING_DEGREE_PATTERN}(?:{down_words})",
        lambda match: replace_market_direction(match, "下行"),
        value,
    )

    state_words = (
        (r"回落|调整", "调整"),
        (r"企稳|稳定|止跌|见底", "趋稳"),
        (r"反弹|回升|回暖|复苏", "修复"),
        (r"承压|疲弱|低迷", "承压"),
    )
    for words, replacement in state_words:
        value = re.sub(
            rf"{HOUSING_PRICE_TERM_PATTERN}(?P<state_modifier>正在|正|持续|逐步|开始|有所)?(?:{words})",
            lambda match, replacement=replacement: (
                _housing_subject(match.group("segment"))
                + (match.group("state_modifier") or "")
                + replacement
            ),
            value,
        )

    value = re.sub(
        rf"(?:偏高|过高|高企|太高|很高|较高|昂贵|高){HOUSING_PRICE_TERM_PATTERN}",
        lambda match: f"较高{_housing_cost(match.group('segment'))}",
        value,
    )
    value = re.sub(
        rf"{HOUSING_PRICE_TERM_PATTERN}(?:偏高|过高|高企|太高|很高|较高|昂贵|高)",
        lambda match: f"{_housing_cost(match.group('segment'))}偏高",
        value,
    )
    value = re.sub(
        rf"(?:偏低|过低|太低|很低|较低|便宜|低){HOUSING_PRICE_TERM_PATTERN}",
        lambda match: f"较低{_housing_cost(match.group('segment'))}",
        value,
    )
    value = re.sub(
        rf"{HOUSING_PRICE_TERM_PATTERN}(?:偏低|过低|太低|很低|较低|便宜|低)",
        lambda match: f"{_housing_cost(match.group('segment'))}较低",
        value,
    )

    # A direct currency amount describes an absolute level.  This includes
    # forms such as “约3万元/平方米”, “是每平米3万元”, and “从3万…”.
    numeric_level_prefix = r"(?:约|大约|接近)?(?:为|是|达到|达|在)?"
    unit_before_amount = r"(?:(?:每(?:平方米|平米)|每㎡)\s*)?"
    value = re.sub(
        rf"{HOUSING_PRICE_TERM_PATTERN}(?=\s*(?:{numeric_level_prefix}{unit_before_amount}"
        rf"{HOUSING_CURRENCY_AMOUNT_PATTERN}|从\s*{HOUSING_CURRENCY_AMOUNT_PATTERN}))",
        lambda match: _housing_level(match.group("segment")),
        value,
    )

    return re.sub(
        HOUSING_PRICE_TERM_PATTERN,
        lambda match: _housing_subject(match.group("segment")),
        value,
    )


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

    # Run this last so earlier crisis/China guards can first turn phrases such
    # as 房价崩盘 into 房价承压; the final pass then removes the direct term.
    value = neutralize_housing_price_wording(value)

    # Clean up awkward repeats after replacement.
    value = value.replace("承压承压", "承压")
    value = value.replace("变化变化", "变化")
    value = value.replace("配置配置", "配置")
    value = value.replace("资管管理", "资管")
    value = value.replace("地产市场市场", "地产市场")
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
        comment = str(clip.get("comment", ""))
        clip["comment_highlights"] = [
            item for item in _sanitize_list(clip["comment_highlights"], context=context, for_title=False)
            if item and (not comment or item in comment)
        ]

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
                comment = str(item.get("comment", ""))
                item["comment_highlights"] = [
                    highlight
                    for highlight in _sanitize_list(
                        item["comment_highlights"], context=context, for_title=False
                    )
                    if highlight and (not comment or highlight in comment)
                ]


def sanitize_plan_wording(plan: dict[str, Any]) -> dict[str, Any]:
    clips = plan.get("clips")
    if isinstance(clips, list):
        for clip in clips:
            if isinstance(clip, dict):
                sanitize_clip_wording(plan, clip)
    return plan
