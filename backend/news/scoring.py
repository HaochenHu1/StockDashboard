"""Layer 1 importance scoring: keyword rules + SEC form rules (always on, unit tested).

Levels: 高 (3), 中 (2), 低 (1).
* A high keyword in the headline -> 高; a medium keyword -> 中.
* A keyword that appears only in the summary / body counts one level lower.
* SEC 8-K with item 2.02 (earnings), 5.02 (officer/director change) or 1.05 (cybersecurity
  incident) is always 高; other 8-K items, 6-K and Schedule 13D are 中; 13G and Form 4 are 低.
* An item whose headline mentions a watched symbol (ticker or company name) gets +0.5 on its
  score, so it ranks above an otherwise equal item that mentions the symbol only elsewhere.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

LEVELS = {3: "高", 2: "中", 1: "低"}
LEVEL_NUM = {"高": 3, "中": 2, "低": 1}

DEFAULT_HIGH_KEYWORDS = [
    "FBI", "DOJ", "SEC investigation", "subpoena", "indictment", "probe", "export control", "Commerce Department",
    "Entity List", "recall", "FDA approval", "FDA rejection", "guidance cut", "guidance raise", "resigns",
    "steps down", "acquisition", "merger", "bankruptcy", "trading halt", "short seller report",
    "调查", "起诉", "出口管制", "下调指引", "上调指引", "辞职", "收购", "破产", "停牌",
]
DEFAULT_MEDIUM_KEYWORDS = [
    "earnings", "quarterly results", "revenue", "guidance", "outlook", "downgrade", "upgrade", "price target",
    "lawsuit", "settlement", "layoffs", "buyback", "dividend", "partnership", "contract", "offering",
    "activist", "stake",
    "财报", "业绩", "营收", "指引", "评级", "目标价", "诉讼", "裁员", "回购", "分红", "合作", "增持", "减持",
]
SEC_ITEM_ZH = {
    "1.01": "签订重大协议", "1.02": "终止重大协议", "1.03": "破产或接管", "1.04": "矿山安全", "1.05": "网络安全事件",
    "2.01": "完成收购或处置资产", "2.02": "业绩公告", "2.03": "新增重大财务义务", "2.04": "触发财务义务",
    "2.05": "退出或处置成本", "2.06": "重大减值", "3.01": "退市或不再符合上市标准", "3.02": "未注册的股权销售",
    "3.03": "证券持有人权利变更", "4.01": "更换审计师", "4.02": "已发布财报不可依赖", "5.01": "控制权变更",
    "5.02": "高管或董事变动", "5.03": "章程修订", "5.05": "道德准则修订", "5.07": "股东投票结果",
    "7.01": "FD 披露", "8.01": "其他事件", "9.01": "财务报表与附件",
}
DEFAULT_SEC_HIGH_ITEMS = ["2.02", "5.02", "1.05"]
_CJK = re.compile(r"[㐀-鿿]")


def _pattern(keyword: str) -> re.Pattern[str]:
    kw = keyword.strip()
    if _CJK.search(kw):
        return re.compile(re.escape(kw))
    # word boundaries; allow simple plural / past-tense endings ("probes", "recalled")
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(kw) + r"(?:s|es|ed|d)?(?![A-Za-z0-9])", re.IGNORECASE)


@dataclass
class KeywordSet:
    high: list[str] = field(default_factory=lambda: list(DEFAULT_HIGH_KEYWORDS))
    medium: list[str] = field(default_factory=lambda: list(DEFAULT_MEDIUM_KEYWORDS))

    def __post_init__(self) -> None:
        self._high = [(k, _pattern(k)) for k in self.high if k.strip()]
        self._medium = [(k, _pattern(k)) for k in self.medium if k.strip()]

    def match(self, text: str) -> tuple[list[str], list[str]]:
        text = text or ""
        return ([k for k, p in self._high if p.search(text)], [k for k, p in self._medium if p.search(text)])


@dataclass
class ScoreResult:
    level: str
    score: float
    matched: list[str]
    reasons: list[str]
    headline_mentions: bool


def mentions(text: str, terms: Iterable[str]) -> bool:
    for t in terms:
        t = (t or "").strip()
        if len(t) < 2:
            continue
        if _CJK.search(t):
            if t in (text or ""):
                return True
        elif re.search(r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])", text or "", re.IGNORECASE):
            return True
    return False


def normalize_form(form: str) -> str:
    f = (form or "").upper().strip()
    return f.replace("SCHEDULE ", "SC ")  # EDGAR renamed SC 13D/G to SCHEDULE 13D/G in Dec 2024


def sec_level(form: str, items: Iterable[str], high_items: Iterable[str] = DEFAULT_SEC_HIGH_ITEMS) -> tuple[int, str]:
    f = normalize_form(form)
    items = [i.strip() for i in items or [] if i and i.strip()]
    if f in ("8-K", "8-K/A"):
        hits = [i for i in items if i in set(high_items)]
        if hits:
            return 3, "8-K Item " + ", ".join(f"{i}（{SEC_ITEM_ZH.get(i, '')}）" for i in hits)
        return 2, "8-K 重大事件"
    if f.startswith("6-K"):
        return 2, "6-K 外国发行人报告"
    if f.startswith("SC 13D"):
        return 2, "13D 持股 5% 以上（可能有意影响公司）"
    if f.startswith("SC 13G"):
        return 1, "13G 被动持股 5% 以上"
    if f in ("4", "4/A"):
        return 1, "Form 4 内部人交易"
    return 1, f"{form} 文件"


def score_item(title: str, summary: str = "", *, kind: str = "news", form: str = "", items: Iterable[str] = (),
               watch_terms: Iterable[str] = (), keywords: KeywordSet | None = None,
               sec_high_items: Iterable[str] = DEFAULT_SEC_HIGH_ITEMS) -> ScoreResult:
    kw = keywords or KeywordSet()
    level, reasons, matched = 1, [], []
    if kind == "filing":
        level, why = sec_level(form, items, sec_high_items)
        reasons.append(why)
    h_hi, h_med = kw.match(title)
    s_hi, s_med = kw.match(summary) if summary else ([], [])
    s_hi = [k for k in s_hi if k not in h_hi]
    s_med = [k for k in s_med if k not in h_med]
    if h_hi:
        level = max(level, 3)
        reasons.append("标题含高重要性关键词：" + "、".join(h_hi))
    elif h_med:
        level = max(level, 2)
        reasons.append("标题含中重要性关键词：" + "、".join(h_med))
    if s_hi:
        level = max(level, 2)
        reasons.append("正文含高重要性关键词：" + "、".join(s_hi))
    elif s_med and level < 2:
        reasons.append("正文含中重要性关键词：" + "、".join(s_med))
    matched = h_hi + h_med + s_hi + s_med
    head = mentions(title, watch_terms)
    score = level + (0.5 if head else 0.0)
    return ScoreResult(LEVELS[level], score, matched, reasons, head)
