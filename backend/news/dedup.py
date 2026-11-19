"""Event deduplication (pure, unit tested).

Two items belong to the same event when they share a symbol, were published within
30 minutes of each other, and either
* point to the same underlying item (same moomoo post id or SEC accession number), or
* have similar normalized headlines (token Jaccard or sequence ratio >= threshold), or
* one of them is an SEC filing and both are about the same kind of event (for example an
  8-K item 2.02 and an earnings headline).
"""
from __future__ import annotations

import html
import re
from difflib import SequenceMatcher
from typing import Iterable

_TAGS = re.compile(r"<[^>]+>")
_PUNCT = re.compile(r"[^\w㐀-鿿]+", re.UNICODE)
_CJK_RUN = re.compile(r"[㐀-鿿]+")
_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "at", "by", "as", "is", "are", "its",
         "it", "from", "after", "over", "into", "says", "say", "inc", "corp", "co", "ltd", "stock", "shares", "us"}
_MOOMOO_ID = re.compile(r"/news/(?:post|notice)/(\d+)")
_SEC_ACC = re.compile(r"(\d{10}-\d{2}-\d{6})")

CATEGORIES = {
    "earnings": ["earnings", "results", "quarter", "q1", "q2", "q3", "q4", "eps", "revenue", "财报", "业绩", "季度"],
    "executive": ["ceo", "cfo", "chief", "resign", "steps down", "appoint", "director", "辞职", "任命", "高管", "董事"],
    "cyber": ["cyber", "breach", "hack", "ransomware", "网络安全", "黑客", "数据泄露"],
    "deal": ["acquisition", "acquire", "merger", "buyout", "takeover", "收购", "合并", "并购"],
    "regulatory": ["fbi", "doj", "sec", "probe", "investigation", "subpoena", "indictment", "调查", "起诉"],
}
SEC_ITEM_CATEGORY = {"2.02": "earnings", "5.02": "executive", "1.05": "cyber", "1.01": "deal", "2.01": "deal"}


def clean_title(title: str) -> str:
    return html.unescape(_TAGS.sub("", title or "")).strip()


def normalize(title: str) -> str:
    return " ".join(_PUNCT.sub(" ", clean_title(title).lower()).split())


def tokens(title: str) -> set[str]:
    norm = normalize(title)
    out = {w for w in norm.split() if len(w) > 1 and w not in _STOP and not _CJK_RUN.fullmatch(w)}
    for run in _CJK_RUN.findall(norm):
        out.update(run[i:i + 2] for i in range(max(1, len(run) - 1)))
    return out


def similarity(a: str, b: str) -> float:
    """max of token Jaccard, character sequence ratio and a damped overlap coefficient.

    The overlap coefficient (shared tokens / tokens of the shorter headline) catches rephrased
    headlines such as "DOJ opens probe into X executive" vs "X executive under DOJ probe".
    """
    ta, tb = tokens(a), tokens(b)
    jac = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    seq = SequenceMatcher(None, normalize(a), normalize(b)).ratio()
    small = min(len(ta), len(tb))
    overlap = 0.8 * len(ta & tb) / small if small >= 4 else 0.0
    return max(jac, seq, overlap)


def url_key(url: str) -> str | None:
    if not url:
        return None
    m = _MOOMOO_ID.search(url)
    if m:
        return f"moomoo:{m.group(1)}"
    m = _SEC_ACC.search(url)
    if m:
        return f"sec:{m.group(1)}"
    return url.split("?", 1)[0].rstrip("/").lower()


def categories(title: str, items: Iterable[str] = ()) -> set[str]:
    text = normalize(title)
    cats = {SEC_ITEM_CATEGORY[i] for i in items or [] if i in SEC_ITEM_CATEGORY}
    for cat, words in CATEGORIES.items():
        if any((w in text) if _CJK_RUN.search(w) else re.search(r"\b" + re.escape(w) + r"\b", text) for w in words):
            cats.add(cat)
    return cats


def same_event(item: dict, event: dict, window_sec: float = 1800, threshold: float = 0.5) -> bool:
    """item: {symbols, published, title, url, kind, items}; event: {symbols, published, last, title, keys, kinds, cats}"""
    if not set(item.get("symbols") or []) & set(event.get("symbols") or []):
        return False
    t = item.get("published") or 0
    lo, hi = event.get("published") or 0, event.get("last") or event.get("published") or 0
    if t < lo - window_sec or t > hi + window_sec:
        return False
    key = url_key(item.get("url", ""))
    if key and key in set(event.get("keys") or []):
        return True
    if similarity(item.get("title", ""), event.get("title", "")) >= threshold:
        return True
    if any(similarity(item.get("title", ""), t2) >= threshold for t2 in event.get("titles") or []):
        return True
    involves_filing = item.get("kind") == "filing" or "filing" in set(event.get("kinds") or [])
    if involves_filing:
        shared = categories(item.get("title", ""), item.get("items") or []) & set(event.get("cats") or [])
        return bool(shared)
    return False


def find_event(item: dict, events: Iterable[dict], window_sec: float = 1800, threshold: float = 0.5) -> dict | None:
    best = None
    for ev in events:
        if same_event(item, ev, window_sec, threshold):
            if best is None or abs((ev.get("published") or 0) - (item.get("published") or 0)) < \
                    abs((best.get("published") or 0) - (item.get("published") or 0)):
                best = ev
    return best
