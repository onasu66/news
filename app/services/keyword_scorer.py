"""キーワード抽出 + SEO スコアリング

RSSから取得した記事を「検索されやすさ × 解説価値」でスコア付けし、
1日あたり上位N件に絞る。

狙いKW・除外パターンは config/seo_keywords.yaml と
動的ストア（auto_boost / promoted）から読む。
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

MIN_CONTENT_LENGTH = 80


def _high_value_keywords() -> set[str]:
    from app.services.seo_keywords_config import get_effective_high_value

    return get_effective_high_value()


def _low_value_categories() -> set[str]:
    from app.services.seo_keywords_config import get_low_value_categories

    return get_low_value_categories()


def _low_value_title_patterns() -> re.Pattern:
    from app.services.seo_keywords_config import get_exclude_title_regex

    return get_exclude_title_regex()


def _search_intent_terms() -> tuple[str, ...]:
    from app.services.seo_keywords_config import get_search_intent_terms

    return get_search_intent_terms()


def _comparison_terms() -> tuple[str, ...]:
    from app.services.seo_keywords_config import get_comparison_terms

    return get_comparison_terms()


def _question_words() -> list[str]:
    from app.services.seo_keywords_config import get_question_words

    return get_question_words()


def _topical_focus() -> dict:
    from app.services.seo_keywords_config import get_topical_focus

    return get_topical_focus()


# 後方互換: 古い import 向け（起動時スナップショット。動的更新は _high_value_keywords() を使う）
try:
    from app.services.seo_keywords_config import get_target_high_value

    HIGH_VALUE_KEYWORDS = get_target_high_value()
except Exception:
    HIGH_VALUE_KEYWORDS = {"AI", "政策", "地震"}

LOW_VALUE_CATEGORIES = {"スポーツ", "エンタメ"}
LOW_VALUE_TITLE_PATTERNS = re.compile(
    r"(号外|訃報|結果|スコア|芸能|ランキング|占い|星座|ゴシップ|"
    r"breaking\s*:?\s*$|score|results|obituary|gossip)",
    re.IGNORECASE,
)
QUESTION_WORDS = ["何", "とは", "いつ", "どうして", "なぜ", "どう", "どこ", "誰"]
SEARCH_INTENT_TERMS = (
    "とは", "なぜ", "理由", "影響", "今後", "いつ", "どこ", "誰", "何",
    "プロフィール", "経歴", "成績", "年俸", "移籍", "結婚", "身長", "年齢",
    "比較", "過去", "データ", "ランキング", "サービス", "使い方",
)
COMPARISON_TERMS = (
    "前年", "昨季", "過去", "比較", "平均", "ランキング", "推移", "変化",
    "最多", "初", "連続", "率", "倍", "％", "%", "位", "試合", "本塁打", "打率",
)


def seo_potential_score(title: str, summary: str, category: str = "") -> float:
    """Search-oriented score: named entity, numbers, comparison material, and query intent."""
    text = f"{title or ''} {summary or ''}"
    if not text.strip():
        return 0.0

    score = 0.0
    lower = text.lower()

    katakana_entities = re.findall(r"[ァ-ヴー]{3,}", text)
    latin_entities = re.findall(r"\b[A-Z][A-Za-z0-9&.+-]{2,}\b", text)
    kanji_entities = re.findall(r"[一-龥]{2,6}", text)
    score += min(8, len(set(katakana_entities + latin_entities)) * 2)
    score += min(4, len(set(kanji_entities)) * 0.5)

    numbers = re.findall(r"\d+(?:\.\d+)?", text)
    score += min(8, len(numbers) * 1.5)

    score += sum(2 for term in _search_intent_terms() if term.lower() in lower)
    score += sum(1.5 for term in _comparison_terms() if term.lower() in lower)

    if len(summary or "") >= 220:
        score += 3
    return score


def _trend_token_match(text: str, trend_keywords: list[str]) -> int:
    """トレンドキーワードを単語に分割してテキストに何個マッチするか返す。"""
    count = 0
    for kw in trend_keywords:
        tokens = [t for t in re.split(r"\s+", kw.strip()) if len(t) >= 2]
        if not tokens:
            continue
        if kw in text:
            count += 2
        else:
            count += sum(1 for t in tokens if t in text)
    return count


def lightweight_filter(
    title: str,
    summary: str,
    category: str,
    trend_keywords: list[str] | None = None,
) -> bool:
    """低価値記事なら False を返す。残すべきなら True。"""
    text = f"{title} {summary}"
    if len(text.strip()) < MIN_CONTENT_LENGTH:
        return False

    high_value = _high_value_keywords()
    focus = _topical_focus()
    topical_required = bool(focus.get("enabled")) and category not in (
        focus.get("exempt_categories") or set()
    )
    on_topic = any(kw in text for kw in high_value)

    # トレンド一致は「狙いの範囲内での救済」。看板と無関係な話題まで
    # 通してしまうと、トレンド経由で一般ニュースが素通りしてしまう。
    if trend_keywords and _trend_token_match(text, trend_keywords) >= 2:
        if not topical_required or on_topic:
            return True

    if category != "研究・論文":
        if _low_value_title_patterns().search(title):
            if not any(kw in text for kw in high_value) and seo_potential_score(title, summary, category) < 14:
                return False
        if category in _low_value_categories():
            if not any(kw in text for kw in high_value) and seo_potential_score(title, summary, category) < 16:
                return False

    # サイトの看板（AI・研究）と無関係な一般ニュースを弾く。
    # 以前はここが無く、低価値カテゴリ以外は素通りしていたため、
    # 台風・花火大会・企業人事のような記事が量産されていた。
    if topical_required and not on_topic:
        threshold = focus.get("min_score_without_match") or 0
        if threshold <= 0 or seo_potential_score(title, summary, category) < threshold:
            return False
    return True


def _is_japanese(text: str) -> bool:
    jp_chars = sum(1 for c in text if "\u3040" <= c <= "\u9fff" or "\uff00" <= c <= "\uffef")
    return jp_chars / max(len(text), 1) > 0.15


def _extract_keywords_japanese(text: str) -> list[str]:
    try:
        import MeCab

        tagger = MeCab.Tagger("-Ochasen")
    except Exception:
        return _extract_keywords_simple(text)

    keywords: list[str] = []
    node = tagger.parseToNode(text)
    while node:
        features = node.feature.split(",")
        surface = node.surface
        if features[0] in ("名詞",) and len(surface) >= 2:
            if features[1] not in ("非自立", "代名詞", "数", "接尾"):
                keywords.append(surface)
        node = node.next
    return keywords


def _extract_keywords_simple(text: str) -> list[str]:
    words: list[str] = []
    for w in re.findall(r"[\u3040-\u9fffー]{2,}|[a-zA-Z]{3,}", text):
        if w not in words:
            words.append(w)
    return words[:40]


def extract_keywords(title: str, summary: str) -> list[str]:
    """記事から 1-gram キーワードを抽出"""
    text = f"{title} {summary}"[:2000]
    if _is_japanese(text):
        kws = _extract_keywords_japanese(text)
    else:
        kws = _extract_keywords_simple(text)
    seen = set()
    out: list[str] = []
    for k in kws:
        kl = k.lower()
        if kl not in seen:
            seen.add(kl)
            out.append(k)
    return out[:30]


def make_ngrams(keywords: list[str], n: int = 2) -> list[str]:
    phrases: list[str] = []
    for i in range(len(keywords) - n + 1):
        phrases.append(" ".join(keywords[i : i + n]))
    return phrases[:20]


def add_question_variants(keywords: list[str]) -> list[str]:
    extras: list[str] = []
    qws = _question_words()[:3]
    for kw in keywords[:5]:
        for qw in qws:
            extras.append(f"{kw} {qw}")
    return extras


def score_keywords_autocomplete(
    keywords_1g: list[str], keywords_2g: list[str], question_variants: list[str]
) -> float:
    """Google Suggest は使わない運用のため常に0を返す。"""
    return 0.0


def score_article(
    title: str,
    summary: str,
    category: str,
    trend_keywords: list[str] | None = None,
    published=None,
) -> float:
    """記事1件のスコアを返す（高いほど良い）。時系列ボーナス：新しいほど加点"""
    from datetime import datetime

    kw_1g = extract_keywords(title, summary)
    kw_2g = make_ngrams(kw_1g)
    q_variants = add_question_variants(kw_1g)

    ac_score = score_keywords_autocomplete(kw_1g, kw_2g, q_variants)

    text = f"{title} {summary}"
    hv_bonus = sum(1 for kw in _high_value_keywords() if kw in text)
    seo_bonus = seo_potential_score(title, summary, category) * 1.5

    # 実績KW（GSC貼り付け）がある場合は追加加点
    perf_bonus = 0
    try:
        from app.services.seo_keywords_store import get_performance_queries

        for q in get_performance_queries()[:80]:
            if len(q) >= 2 and q.lower() in text.lower():
                perf_bonus += 3
    except Exception:
        pass

    trend_bonus = 0
    if trend_keywords:
        text_lower = text.lower()
        for kw in trend_keywords:
            tokens = [t for t in re.split(r"\s+", kw.strip()) if len(t) >= 2]
            if not tokens:
                continue
            if kw.lower() in text_lower:
                trend_bonus += 10
            else:
                token_hits = sum(1 for t in tokens if t.lower() in text_lower)
                trend_bonus += token_hits * 4

    recency_bonus = 0.0
    if published is not None:
        try:
            now = datetime.now()
            if hasattr(published, "timestamp"):
                delta = (now - published).total_seconds()
            else:
                delta = 0
            hours_ago = delta / 3600.0
            recency_bonus = max(0.0, 15.0 - hours_ago * 0.5)
        except Exception:
            pass

    return ac_score + hv_bonus + seo_bonus + perf_bonus + trend_bonus + recency_bonus


def rank_and_filter_articles(
    items: list,
    trend_keywords: list[str] | None = None,
    max_articles: int = 20,
) -> list:
    """RSS記事リストを軽量フィルタ → スコア → 上位N件に絞る。"""
    filtered = [
        item
        for item in items
        if lightweight_filter(item.title, item.summary, item.category, trend_keywords)
    ]
    logger.info("軽量フィルタ: %d → %d件", len(items), len(filtered))

    if not filtered:
        return items[:max_articles]

    scored: list[tuple[float, object]] = []
    for item in filtered:
        try:
            pub = getattr(item, "published", None)
            s = score_article(item.title, item.summary, item.category, trend_keywords, published=pub)
        except Exception:
            s = 0.0
        scored.append((s, item))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [item for _, item in scored[:max_articles]]
    logger.info(
        "スコア上位 %d件を抽出（最高 %.1f / 最低 %.1f）",
        len(top),
        scored[0][0] if scored else 0,
        scored[min(max_articles - 1, len(scored) - 1)][0] if scored else 0,
    )
    return top
