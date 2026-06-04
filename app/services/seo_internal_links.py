"""記事ページ向けの内部リンク（関連・一覧）選定。"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime

_JA_STOP = frozenset(
    "の に は を が で と も へ や から まで より という こと ため よう する した して される ない ある いる なる れる られる について による として など これ それ あの その この へ 的 へ向け".split()
)
_TOKEN_RE = re.compile(r"[一-龯ぁ-んァ-ヶa-zA-Z0-9]{2,}")


def _normalize_text(text: str) -> str:
    t = unicodedata.normalize("NFKC", (text or ""))
    return re.sub(r"\s+", " ", t).strip()


def _extract_keywords(title: str, summary: str = "") -> set[str]:
    blob = _normalize_text(f"{title} {summary}")
    out: set[str] = set()
    for m in _TOKEN_RE.finditer(blob):
        w = m.group(0)
        if len(w) < 2 or w in _JA_STOP:
            continue
        if w.isascii() and len(w) < 3:
            continue
        out.add(w.lower() if w.isascii() else w)
    return out


def _sort_key(item) -> datetime:
    for attr in ("added_at", "published"):
        dt = getattr(item, attr, None)
        if dt and hasattr(dt, "timestamp"):
            try:
                return dt
            except Exception:
                pass
    return datetime.min


def pick_related_articles(current, candidates: list, *, limit: int = 6) -> list:
    """タイトル・要約の語重なり＋カテゴリで関連記事を選ぶ。"""
    cur_id = getattr(current, "id", None)
    cur_kw = _extract_keywords(getattr(current, "title", ""), getattr(current, "summary", ""))
    scored: list[tuple[float, object]] = []
    for a in candidates or []:
        if not a or getattr(a, "id", None) == cur_id:
            continue
        score = 0.0
        if (getattr(a, "category", "") or "") == (getattr(current, "category", "") or ""):
            score += 2.0
        a_kw = _extract_keywords(getattr(a, "title", ""), getattr(a, "summary", ""))
        overlap = len(cur_kw & a_kw) if cur_kw and a_kw else 0
        score += overlap * 1.5
        if overlap == 0:
            continue
        scored.append((score, a))
    scored.sort(key=lambda x: (x[0], _sort_key(x[1])), reverse=True)
    out: list = []
    seen: set[str] = set()
    for _, a in scored:
        aid = getattr(a, "id", "")
        if not aid or aid in seen:
            continue
        seen.add(aid)
        out.append(a)
        if len(out) >= limit:
            break
    if len(out) < min(3, limit):
        for a in sorted(candidates or [], key=_sort_key, reverse=True):
            aid = getattr(a, "id", "")
            if not aid or aid == cur_id or aid in seen:
                continue
            if (getattr(a, "category", "") or "") != (getattr(current, "category", "") or ""):
                continue
            seen.add(aid)
            out.append(a)
            if len(out) >= limit:
                break
    return out


def pick_same_category_articles(current, candidates: list, exclude_ids: set[str], *, limit: int = 4) -> list:
    """同一カテゴリの記事（関連と重複しない）。"""
    cur_id = getattr(current, "id", None)
    cat = (getattr(current, "category", "") or "").strip()
    out: list = []
    for a in sorted(candidates or [], key=_sort_key, reverse=True):
        aid = getattr(a, "id", "")
        if not aid or aid == cur_id or aid in exclude_ids:
            continue
        if (getattr(a, "category", "") or "").strip() != cat:
            continue
        out.append(a)
        if len(out) >= limit:
            break
    return out


def pick_latest_articles(current, candidates: list, exclude_ids: set[str], *, limit: int = 5) -> list:
    """最新記事（現在・関連・同カテゴリ枠を除く）。"""
    cur_id = getattr(current, "id", None)
    out: list = []
    for a in sorted(candidates or [], key=_sort_key, reverse=True):
        aid = getattr(a, "id", "")
        if not aid or aid == cur_id or aid in exclude_ids:
            continue
        out.append(a)
        if len(out) >= limit:
            break
    return out


def list_hub_for_article(item) -> tuple[str, str]:
    """カテゴリに応じた一覧ページ（ラベル, path）。"""
    if (getattr(item, "category", "") or "").strip() == "研究・論文":
        return "論文一覧", "/"
    return "ニュース一覧", "/news"
