"""話題まとめ記事生成サービス

Googleトレンドのキーワード1つに対して複数ソースの記事を集め、
「○○ まとめ・最新情報」として1本の記事にまとめる。

フロー:
  1. トレンドKWごとに Google News RSS で関連記事を3〜6本収集
  2. 各記事の本文をフェッチ（取れた分だけ）
  3. 複数本文を結合し expand_navigator_to_article で1本生成
  4. 既存パイプライン（NewsItem → process_rss_to_site_article）と同じ形式で保存

呼び出し元:
  - claude_researcher.py の curation_v2 フロー内でサブ処理として呼ぶ
  - run_claude_research_scheduler.py から直接呼ぶことも可
"""
import hashlib
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
JST = ZoneInfo("Asia/Tokyo")

# 1話題あたり収集するソース数（多すぎるとトークンが膨れる）
DIGEST_SOURCES_PER_TOPIC = 4
# まとめ記事として生成する話題の最大数（1回の実行で）
DIGEST_MAX_TOPICS = 5
# 1ソースあたり使う本文の最大文字数
DIGEST_BODY_PER_SOURCE = 1500


# --------------------------------------------------------------------------- #
#  話題クラスタリング（同じ話題を束ねる）
# --------------------------------------------------------------------------- #

def _topic_tokens(keyword: str) -> set[str]:
    """キーワードをトークン分割して集合で返す（2文字以上）"""
    tokens = re.split(r"[\s　・/]", keyword.strip())
    return {t for t in tokens if len(t) >= 2}


def cluster_by_topic(
    news_items: list[dict],
    min_cluster_size: int = 2,
) -> list[dict]:
    """
    同じトレンドキーワード（keyword フィールド）でグループ化し、
    2件以上ある話題だけを「まとめ候補」として返す。

    返り値: [{"keyword": "ワールドカップ", "articles": [...]}, ...]
    """
    from collections import defaultdict

    # keyword でグループ化
    groups: dict[str, list[dict]] = defaultdict(list)
    for item in news_items:
        kw = (item.get("keyword") or "").strip()
        if kw:
            groups[kw].append(item)

    clusters = []
    for kw, articles in groups.items():
        if len(articles) >= min_cluster_size:
            clusters.append({
                "keyword": kw,
                "articles": articles[:DIGEST_SOURCES_PER_TOPIC],
                "count": len(articles),
            })

    # 記事数が多い順（話題性が高い順）
    clusters.sort(key=lambda c: c["count"], reverse=True)
    return clusters[:DIGEST_MAX_TOPICS]


# --------------------------------------------------------------------------- #
#  まとめ記事タイトル生成
# --------------------------------------------------------------------------- #

def _build_digest_title(keyword: str, article_titles: list[str]) -> str:
    """
    「○○ まとめ最新2026」のようなタイトルを生成する。
    元記事タイトルのキーワードをもとに補強する。
    """
    today = datetime.now(JST)
    year = today.year
    # 「ワールドカップ」「W杯」「FIFA」など題名内の別キーワードを抽出して補強
    extra_kws: list[str] = []
    for t in article_titles[:3]:
        for tok in re.split(r"[　\s・/・]", t):
            tok = tok.strip()
            if len(tok) >= 3 and tok not in keyword and tok not in extra_kws:
                extra_kws.append(tok)
            if len(extra_kws) >= 2:
                break

    suffix = "・".join(extra_kws[:2]) if extra_kws else "最新情報"
    title = f"{keyword} まとめ─{suffix}【{year}年最新】"
    # 42文字以内に収める
    if len(title) > 42:
        title = f"{keyword} 最新まとめ【{year}年】"
    return title


# --------------------------------------------------------------------------- #
#  まとめ記事の本文素材を構築
# --------------------------------------------------------------------------- #

def _fetch_bodies(articles: list[dict]) -> str:
    """
    複数記事の本文をフェッチして結合する。
    取れなかった記事はスキップ。
    """
    from app.services.article_fetcher import fetch_article_body
    from app.services.rss_service import sanitize_display_text

    parts: list[str] = []
    for art in articles:
        url = art.get("url", "")
        title = art.get("title", "")
        if not url:
            continue
        try:
            body = fetch_article_body(url)
            if body:
                clean = sanitize_display_text(body)[:DIGEST_BODY_PER_SOURCE]
                parts.append(f"【{title}】（{art.get('source', '')}）\n{clean}")
        except Exception as e:
            logger.debug("本文フェッチ失敗 (%s): %s", url[:60], e)

    return "\n\n---\n\n".join(parts)


# --------------------------------------------------------------------------- #
#  まとめ記事1本を生成して保存
# --------------------------------------------------------------------------- #

def generate_digest_article(cluster: dict) -> bool:
    """
    話題クラスタ1件からまとめ記事を生成してサイトに保存する。
    成功したら True を返す。

    既存の process_rss_to_site_article パイプラインを使うが、
    summary に複数ソース本文を詰め込むことで、generate_all_explanations が
    より豊かな素材でまとめ記事を作れるようにする。
    """
    from app.services.rss_service import NewsItem
    from app.services.article_processor import process_rss_to_site_article

    keyword = cluster["keyword"]
    articles = cluster["articles"]
    if not articles:
        return False

    logger.info("話題まとめ記事生成: 「%s」(%d件のソース)", keyword, len(articles))

    # タイトル生成
    art_titles = [a.get("title", "") for a in articles]
    title = _build_digest_title(keyword, art_titles)

    # 複数ソースの本文を結合
    combined_body = _fetch_bodies(articles)
    if not combined_body:
        # 本文が取れなければ各タイトル＋summaryの寄せ集めで代替
        fallback_parts = []
        for a in articles:
            t = a.get("title", "")
            s = a.get("summary", "")
            if t:
                fallback_parts.append(f"【{t}】\n{s}" if s else f"【{t}】")
        combined_body = "\n\n".join(fallback_parts)

    if not combined_body.strip():
        logger.warning("まとめ記事: 本文素材なしでスキップ (%s)", keyword)
        return False

    # 素材文字数チェック（最低400字）
    if len(combined_body) < 400:
        logger.warning("まとめ記事: 素材が短すぎます (%d字) (%s)", len(combined_body), keyword)
        return False

    # 代表URLとソース（最初の記事を採用）
    rep = articles[0]
    rep_url = rep.get("url", "")
    source_names = [a.get("source", "") for a in articles[:3] if a.get("source")]
    rep_source = f"複数ソース（{' / '.join(source_names)}）" if source_names else "複数ソース"
    category = rep.get("category", "国内")

    # 一意のID（keyword + 日付）
    date_str = datetime.now(JST).strftime("%Y%m%d")
    item_id = "dg-" + hashlib.md5(f"{keyword}{date_str}".encode()).hexdigest()[:14]

    # まとめ用サマリー: 「まとめ記事である旨の導入 + 各記事本文」
    # process_rss_to_site_article 内で fetch_article_body(rep_url) が走るが、
    # summary に本文を詰めているため is_source_material_sufficient はパスする
    intro = (
        f"「{keyword}」についての最新情報を複数メディア（{', '.join(source_names[:2])}など）の"
        f"報道をもとにまとめた記事です。\n\n"
    )
    combined_summary = intro + combined_body[:1400]

    news_item = NewsItem(
        id=item_id,
        title=title,
        link=rep_url,
        summary=combined_summary,
        published=datetime.now(JST).replace(tzinfo=None),
        source=rep_source,
        category=category,
        image_url=rep.get("image_url"),
    )

    ok = process_rss_to_site_article(news_item, force=False)
    if ok:
        logger.info("まとめ記事生成 OK: %s", title[:50])
    else:
        logger.info("まとめ記事: スキップまたは品質基準未達 (%s)", keyword)
    return ok


# --------------------------------------------------------------------------- #
#  メインエントリ：トレンド別まとめ記事を一括生成
# --------------------------------------------------------------------------- #

def run_topic_digest(
    news_candidates: list[dict],
    max_topics: int = DIGEST_MAX_TOPICS,
) -> int:
    """
    news_candidates（keyword フィールド付き記事リスト）から
    話題別まとめ記事を生成してサイトに保存する。
    成功件数を返す。

    呼び出し例:
        from app.services.topic_digest import run_topic_digest
        count = run_topic_digest(news_candidates)
    """
    clusters = cluster_by_topic(news_candidates, min_cluster_size=2)
    if not clusters:
        logger.info("話題まとめ: 2件以上ある話題なし、スキップ")
        return 0

    logger.info("話題まとめ: %d件の話題クラスタを検出", len(clusters))
    count = 0
    for cluster in clusters[:max_topics]:
        try:
            ok = generate_digest_article(cluster)
            if ok:
                count += 1
        except Exception as e:
            logger.error("話題まとめ生成エラー (%s): %s", cluster.get("keyword"), e)

    logger.info("話題まとめ記事生成完了: %d件", count)
    return count
