"""既存 RSS フィードから論文候補を収集する（Claude WebSearch 不要）

rss_service.py が持つ arXiv / PubMed / Nature 等のフィードを直接使い、
Claude に渡す論文候補リストを事前に作る。
Claude は「選ぶだけ」になるため WebSearch ターンを消費しない。
"""
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
JST = ZoneInfo("Asia/Tokyo")

# Claude 選定で優先するテーマに対応するフィード（重みを絞って品質を上げる）
_PAPER_FEEDS: list[tuple[str, str]] = [
    # AI・機械学習（最重要）
    ("https://export.arxiv.org/rss/cs.AI", "arXiv cs.AI"),
    ("https://export.arxiv.org/rss/cs.LG", "arXiv cs.LG"),
    ("https://export.arxiv.org/rss/cs.CL", "arXiv cs.CL"),
    ("https://export.arxiv.org/rss/cs.CV", "arXiv cs.CV"),
    # 量子・宇宙・物理
    ("https://export.arxiv.org/rss/quant-ph", "arXiv quant-ph"),
    ("https://export.arxiv.org/rss/astro-ph", "arXiv astro-ph"),
    # 医療・健康・生命科学
    ("https://export.arxiv.org/rss/q-bio", "arXiv q-bio"),
    ("https://connect.biorxiv.org/biorxiv_xml.php?subject=all", "bioRxiv"),
    ("https://connect.medrxiv.org/medrxiv_xml.php?subject=all", "medRxiv"),
    ("https://bmjopen.bmj.com/rss/current.xml", "BMJ Open"),
    # 総合科学
    ("https://www.nature.com/nature.rss", "Nature"),
    ("https://journals.plos.org/plosone/feed/atom", "PLOS ONE"),
]


def _fetch_feed(url: str, source: str, max_items: int) -> list[dict]:
    """1 フィードから論文候補 dict のリストを返す。"""
    try:
        import feedparser, httpx
        resp = httpx.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; NewsSite/1.0)"},
            timeout=20.0,
            follow_redirects=True,
        )
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)

        results: list[dict] = []
        for entry in feed.entries[:max_items]:
            link = (entry.get("link") or "").strip()
            title = (entry.get("title") or "").strip()
            if not link or not title:
                continue

            # 発行日
            pub_iso = ""
            if entry.get("published_parsed"):
                try:
                    pub_iso = datetime(*entry.published_parsed[:6]).isoformat()
                except Exception:
                    pass
            if not pub_iso:
                pub_iso = datetime.now(JST).replace(tzinfo=None).isoformat()

            # 概要（短く、reason 生成の補助用）
            summary = (entry.get("summary") or "").strip()[:300]
            # HTMLタグ除去
            import re
            summary = re.sub(r"<[^>]+>", " ", summary).strip()

            item_id = "pr-" + hashlib.md5(link.encode()).hexdigest()[:14]
            results.append({
                "id": item_id,
                "title": title,
                "url": link,
                "source": source,
                "category": "研究・論文",
                "published": pub_iso,
                "summary": summary,
                "image_url": None,
            })
        return results
    except Exception as e:
        logger.debug("論文フィード取得失敗 (%s): %s", source, e)
        return []


def fetch_paper_candidates(
    max_per_feed: int = 5,
    max_total: int = 40,
) -> list[dict]:
    """
    論文 RSS フィードを並列取得して候補リストを返す。

    max_per_feed: 1 フィードあたりの最大取得件数
    max_total   : 返す論文候補の総上限
    """
    seen_urls: set[str] = set()
    all_results: list[dict] = []

    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {
            ex.submit(_fetch_feed, url, source, max_per_feed): source
            for url, source in _PAPER_FEEDS
        }
        for future in as_completed(futures):
            try:
                items = future.result()
                for item in items:
                    url = item["url"]
                    if url not in seen_urls:
                        seen_urls.add(url)
                        all_results.append(item)
            except Exception:
                pass

    logger.info("論文 RSS: %d 件の候補を収集", len(all_results))
    return all_results[:max_total]
