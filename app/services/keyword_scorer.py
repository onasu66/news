"""キーワード抽出 + Google Autocomplete スコアリング

RSSから取得した記事を「検索されやすさ × 解説価値」でスコア付けし、
1日あたり上位N件に絞る。

流れ:
  1. 軽量フィルタで低価値記事を除外
  2. 記事タイトル＋本文からキーワード抽出（1-gram / 2-gram）
  3. Google Autocomplete API でキーワードの検索ポテンシャルを計測
  4. 疑問系ワードにはボーナス
  5. スコア上位の記事を返す
"""
import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# --- 軽量フィルタ -----------------------------------------------------------

MIN_CONTENT_LENGTH = 80

LOW_VALUE_CATEGORIES = {"スポーツ", "エンタメ"}

# 速報は除外しない（速報を許可するため「速報」はパターンから外している）
LOW_VALUE_TITLE_PATTERNS = re.compile(
    r"(号外|訃報|結果|スコア|芸能|ランキング|占い|星座|ゴシップ|"
    r"breaking\s*:?\s*$|score|results|obituary|gossip)",
    re.IGNORECASE,
)

HIGH_VALUE_KEYWORDS = {
    # 政治・経済・社会制度
    "政策", "法案", "経済", "規制", "金利", "インフレ", "GDP", "予算", "制裁",
    "半導体", "AI", "量子", "脱炭素", "再生可能エネルギー", "外交", "安全保障",
    "サミット", "条約", "改革", "選挙", "判決", "裁判", "汚職", "調査",
    # 健康・科学
    "体重", "筋肉", "老化", "健康", "運動", "栄養", "睡眠", "予防",
    # 災害・緊急事態（これらは検索急増しやすい高価値ニュース）
    "地震", "噴火", "津波", "台風", "洪水", "土砂崩れ", "大雨", "警報", "避難",
    "震度", "マグニチュード", "活火山", "火山", "富士山", "南海トラフ",
    "原発", "放射線", "停電", "断水", "被害", "救助", "行方不明",
    # 英語
    "policy", "regulation", "economy", "inflation", "legislation", "sanctions",
    "semiconductor", "quantum", "climate", "diplomacy", "summit", "reform",
    "election", "ruling", "investigation",
    "muscle", "aging", "protein", "exercise", "diet", "health", "longevity",
    "earthquake", "eruption", "tsunami", "typhoon", "flood", "disaster", "evacuation",
}

QUESTION_WORDS = ["何", "とは", "いつ", "どうして", "なぜ", "どう", "どこ", "誰"]


def _trend_token_match(text: str, trend_keywords: list[str]) -> int:
    """トレンドキーワードを単語に分割してテキストに何個マッチするか返す。
    フレーズ一致(例:「富士山 地震」→ 'text' に '富士山' と '地震' が両方あるか)に対応。"""
    count = 0
    for kw in trend_keywords:
        tokens = [t for t in re.split(r"\s+", kw.strip()) if len(t) >= 2]
        if not tokens:
            continue
        # フレーズ全体が完全一致 → 2点
        if kw in text:
            count += 2
        # 各トークンが個別に含まれている → 1点/トークン
        else:
            count += sum(1 for t in tokens if t in text)
    return count


def lightweight_filter(
    title: str,
    summary: str,
    category: str,
    trend_keywords: list[str] | None = None,
) -> bool:
    """低価値記事なら False を返す。残すべきなら True。
    研究・論文はLOW_VALUE_TITLE_PATTERNS（results/score等）を適用しない。学術論文では一般的な語のため。
    trend_keywords: トレンドに強くマッチする記事はカテゴリフィルタをバイパスする。"""
    text = f"{title} {summary}"
    if len(text.strip()) < MIN_CONTENT_LENGTH:
        return False

    # トレンドに2トークン以上マッチ → 強制通過（カテゴリ・パターン不問）
    if trend_keywords and _trend_token_match(text, trend_keywords) >= 2:
        return True

    if category != "研究・論文":
        if LOW_VALUE_TITLE_PATTERNS.search(title):
            if not any(kw in text for kw in HIGH_VALUE_KEYWORDS):
                return False
        if category in LOW_VALUE_CATEGORIES:
            if not any(kw in text for kw in HIGH_VALUE_KEYWORDS):
                return False
    return True


# --- キーワード抽出（形態素解析 / 簡易トークナイズ） -------------------------

def _is_japanese(text: str) -> bool:
    jp_chars = sum(1 for c in text if '\u3040' <= c <= '\u9fff' or '\uff00' <= c <= '\uffef')
    return jp_chars / max(len(text), 1) > 0.15


def _extract_keywords_japanese(text: str) -> list[str]:
    """日本語テキストから名詞・固有名詞を抽出（形態素解析）"""
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
    """形態素解析が使えないときの簡易抽出"""
    words: list[str] = []
    for w in re.findall(r'[\u3040-\u9fffー]{2,}|[a-zA-Z]{3,}', text):
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
    """隣接 n 単語を結合したフレーズを作る"""
    phrases: list[str] = []
    for i in range(len(keywords) - n + 1):
        phrase = " ".join(keywords[i:i + n])
        phrases.append(phrase)
    return phrases[:20]


def add_question_variants(keywords: list[str]) -> list[str]:
    """疑問系ワードを先頭に付けたバリエーションを追加"""
    extras: list[str] = []
    for kw in keywords[:5]:
        for qw in QUESTION_WORDS[:3]:
            extras.append(f"{kw} {qw}")
    return extras


def score_keywords_autocomplete(keywords_1g: list[str], keywords_2g: list[str],
                                 question_variants: list[str]) -> float:
    """Google Suggest は使わない運用のため常に0を返す。"""
    return 0.0


# --- メインのスコアリングパイプライン ----------------------------------------

def score_article(title: str, summary: str, category: str,
                  trend_keywords: list[str] | None = None,
                  published=None) -> float:
    """記事1件のスコアを返す（高いほど良い）。時系列ボーナス：新しいほど加点"""
    from datetime import datetime
    kw_1g = extract_keywords(title, summary)
    kw_2g = make_ngrams(kw_1g)
    q_variants = add_question_variants(kw_1g)

    ac_score = score_keywords_autocomplete(kw_1g, kw_2g, q_variants)

    text = f"{title} {summary}"
    hv_bonus = sum(1 for kw in HIGH_VALUE_KEYWORDS if kw in text)  # 軽め（1キーワード=+1点）

    trend_bonus = 0
    if trend_keywords:
        text_lower = text.lower()
        for kw in trend_keywords:
            tokens = [t for t in re.split(r"\s+", kw.strip()) if len(t) >= 2]
            if not tokens:
                continue
            # フレーズ完全一致: +10（非常に強いシグナル）
            if kw.lower() in text_lower:
                trend_bonus += 10
            else:
                # トークン個別マッチ: +4/トークン（「富士山」「地震」それぞれが記事に含まれる場合）
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
            # 24時間以内は最大+15点、それ以降は減衰（新しいニュースを優先）
            recency_bonus = max(0.0, 15.0 - hours_ago * 0.5)
        except Exception:
            pass

    return ac_score + hv_bonus + trend_bonus + recency_bonus


def rank_and_filter_articles(
    items: list,
    trend_keywords: list[str] | None = None,
    max_articles: int = 20,
) -> list:
    """
    RSS記事リストを軽量フィルタ → Autocomplete スコア → 上位N件に絞る。
    items は NewsItem のリスト。
    """
    filtered = [
        item for item in items
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
    logger.info("スコア上位 %d件を抽出（最高 %.1f / 最低 %.1f）",
                len(top),
                scored[0][0] if scored else 0,
                scored[min(max_articles - 1, len(scored) - 1)][0] if scored else 0)
    return top
