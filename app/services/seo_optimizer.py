"""SEO 監視・自動最適化ジョブ。

新記事・トレンド・実績KWから auto_boost を更新する。
記事本文のリライトは行わない。
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _parse_dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _load_recent_articles(lookback_days: int) -> list[Any]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    articles: list[Any] = []
    try:
        from app.services.article_cache import load_all

        articles = list(load_all() or [])
    except Exception as e:
        logger.warning("seo_optimizer: 記事読込失敗: %s", e)
        return []

    recent = []
    for a in articles:
        dt = _parse_dt(getattr(a, "added_at", None)) or _parse_dt(getattr(a, "published", None))
        if dt is None or dt >= cutoff:
            recent.append(a)
    return recent


def _trend_keywords() -> list[str]:
    out: list[str] = []
    try:
        from app.services.news_aggregator import NewsAggregator

        trends = NewsAggregator.get_trends(force_refresh=False) or []
        for t in trends:
            kw = getattr(t, "keyword", None) or (t.get("keyword") if isinstance(t, dict) else None)
            if kw:
                out.append(str(kw).strip())
    except Exception as e:
        logger.debug("seo_optimizer: trends 取得スキップ: %s", e)
    return [k for k in out if k]


def _extract_from_articles(articles: list[Any]) -> Counter:
    from app.services.keyword_scorer import extract_keywords

    counter: Counter = Counter()
    for a in articles:
        title = getattr(a, "title", "") or ""
        summary = getattr(a, "summary", "") or ""
        for kw in extract_keywords(title, summary):
            if len(kw) < 2:
                continue
            # 汎用語を軽く抑制
            if kw in {"こと", "もの", "ため", "よう", "これ", "それ", "さん", "記事", "ニュース"}:
                continue
            counter[kw] += 1
    return counter


def _check_sitemap_health() -> dict[str, Any]:
    health: dict[str, Any] = {"sitemap_ok": False, "sitemap_error": None, "url_count": 0}
    try:
        from app.config import settings
        from app.services.sitemap_service import render_sitemap, select_sitemap_articles
        from app.services.article_cache import load_all

        site_url = (getattr(settings, "SITE_URL", "") or "").strip().rstrip("/")
        if not site_url:
            health["sitemap_error"] = "SITE_URL unset"
            return health
        articles = list(load_all() or [])
        xml = render_sitemap(site_url, articles, persist=True)
        if not xml:
            health["sitemap_error"] = "render returned empty"
            return health
        health["sitemap_ok"] = True
        health["url_count"] = xml.count("<url>")
        health["selected_articles"] = len(select_sitemap_articles(articles))
    except Exception as e:
        health["sitemap_error"] = f"{type(e).__name__}: {e}"
        logger.warning("seo_optimizer: sitemap health 失敗: %s", e)
    return health


def _check_indexnow_health() -> dict[str, Any]:
    try:
        from app.config import settings

        key = (getattr(settings, "INDEXNOW_KEY", "") or "").strip()
        enabled = str(getattr(settings, "INDEXNOW_ENABLED", "true") or "true").lower() not in (
            "0",
            "false",
            "no",
        )
        return {
            "indexnow_configured": bool(key),
            "indexnow_enabled": enabled and bool(key),
            "indexnow_key_len": len(key) if key else 0,
        }
    except Exception as e:
        return {"indexnow_configured": False, "indexnow_error": str(e)}


def run_seo_optimize(*, force: bool = False) -> dict[str, Any]:
    """監視ジョブ本体。戻り値はダッシュボード用サマリ。"""
    from app.services.seo_keywords_config import get_optimizer_settings, get_target_high_value
    from app.services.seo_keywords_store import (
        get_performance_queries,
        set_health,
        set_last_candidates,
        update_auto_boost,
        load_state,
    )

    opt = get_optimizer_settings()
    lookback = int(opt.get("lookback_days") or 7)
    min_hits = int(opt.get("min_article_hits") or 2)
    max_boost = int(opt.get("max_auto_boost") or 40)
    require_signal = bool(opt.get("require_trend_or_performance", True))

    articles = _load_recent_articles(lookback)
    freq = _extract_from_articles(articles)
    trends = _trend_keywords()
    trend_lower = {t.lower() for t in trends}
    # トレンドフレーズのトークンもマッチ対象に
    trend_tokens: set[str] = set()
    for t in trends:
        for part in t.replace("　", " ").split():
            if len(part) >= 2:
                trend_tokens.add(part.lower())

    perf_queries = get_performance_queries()
    perf_lower = {q.lower() for q in perf_queries}
    fixed = {k.lower() for k in get_target_high_value()}

    candidates: list[dict[str, Any]] = []
    for kw, hits in freq.most_common(120):
        kl = kw.lower()
        in_trend = kl in trend_lower or kl in trend_tokens or any(kl in t for t in trend_lower)
        in_perf = kl in perf_lower or any(kl in p for p in perf_lower)
        sources: list[str] = ["articles"]
        if in_trend:
            sources.append("trend")
        if in_perf:
            sources.append("performance")
        if hits < min_hits and not in_trend and not in_perf:
            continue
        if require_signal and not in_trend and not in_perf:
            # 高頻度だけは候補表示用に残すが、自動採用はしない
            candidates.append(
                {
                    "keyword": kw,
                    "hits": hits,
                    "sources": sources,
                    "eligible": False,
                    "reason": "no_trend_or_performance",
                }
            )
            continue
        if kl in fixed:
            candidates.append(
                {
                    "keyword": kw,
                    "hits": hits,
                    "sources": sources + ["already_fixed"],
                    "eligible": False,
                    "reason": "already_in_yaml",
                }
            )
            continue
        candidates.append(
            {
                "keyword": kw,
                "hits": hits,
                "sources": sources,
                "eligible": True,
                "reason": "ok",
            }
        )

    # 実績KWで記事出現がなくても、clicks が高いものは候補に追加
    for q in perf_queries[:50]:
        if not q or any(c["keyword"].lower() == q.lower() for c in candidates):
            continue
        candidates.append(
            {
                "keyword": q,
                "hits": 0,
                "sources": ["performance"],
                "eligible": True,
                "reason": "performance_only",
            }
        )

    eligible = [c for c in candidates if c.get("eligible")]
    # 優先: performance > trend > hits
    def _rank(c: dict[str, Any]) -> tuple:
        src = set(c.get("sources") or [])
        return (
            1 if "performance" in src else 0,
            1 if "trend" in src else 0,
            int(c.get("hits") or 0),
        )

    eligible.sort(key=_rank, reverse=True)
    boost_entries = [
        {
            "keyword": c["keyword"],
            "hits": c.get("hits") or 0,
            "sources": c.get("sources") or [],
        }
        for c in eligible[:max_boost]
    ]

    state = update_auto_boost(boost_entries, log_changes=True)
    set_last_candidates(candidates[:80])

    sitemap_h = _check_sitemap_health()
    indexnow_h = _check_indexnow_health()
    health = {**sitemap_h, **indexnow_h, "recent_articles": len(articles), "force": force}
    set_health(health)

    summary = {
        "ok": True,
        "recent_articles": len(articles),
        "candidates": len(candidates),
        "auto_boost": len(state.get("auto_boost") or []),
        "health": health,
        "last_run_at": state.get("last_run_at"),
    }
    logger.info(
        "SEO最適化完了: articles=%d candidates=%d auto_boost=%d sitemap_ok=%s",
        summary["recent_articles"],
        summary["candidates"],
        summary["auto_boost"],
        health.get("sitemap_ok"),
    )
    return summary


def run_gsc_sync(*, days: int = 28) -> dict[str, Any]:
    """Search Console から実績を取得し、performance と rank_history を更新。

    認証未設定でも例外を投げず {"ok": False, "error": ...} を返す。
    """
    from app.services.gsc_client import (
        GscNotConfigured,
        fetch_query_stats,
        fetch_totals,
    )
    from app.services.seo_keywords_store import (
        append_rank_snapshot,
        save_performance_rows,
    )

    try:
        rows = fetch_query_stats(days=days, row_limit=500)
        totals = fetch_totals(days=days)
    except GscNotConfigured as e:
        return {"ok": False, "error": str(e), "configured": False}
    except PermissionError as e:
        return {"ok": False, "error": str(e), "configured": True}
    except Exception as e:
        logger.warning("GSC 同期失敗: %s", e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "configured": True}

    save_performance_rows(rows, source="gsc")
    append_rank_snapshot(rows, totals=totals, source="gsc")

    return {
        "ok": True,
        "configured": True,
        "queries": len(rows),
        "totals": totals,
        "days": days,
    }


def _totals_series() -> list[dict[str, Any]]:
    """rank_history からサイト全体の推移を取り出す（グラフ用）。"""
    from app.services.seo_keywords_store import get_rank_history

    series: list[dict[str, Any]] = []
    for snap in get_rank_history():
        totals = snap.get("totals") or {}
        rows = snap.get("rows") or []
        if totals.get("impressions") is not None:
            clicks = int(totals.get("clicks") or 0)
            impressions = int(totals.get("impressions") or 0)
            position = float(totals.get("position") or 0)
        else:
            # 貼り付け由来など totals が無い場合は行から概算する
            clicks = sum(int(r.get("clicks") or 0) for r in rows)
            impressions = sum(int(r.get("impressions") or 0) for r in rows)
            ranked = [float(r.get("position") or 0) for r in rows if float(r.get("position") or 0) > 0]
            position = round(sum(ranked) / len(ranked), 1) if ranked else 0.0
        series.append(
            {
                "date": snap.get("date"),
                "clicks": clicks,
                "impressions": impressions,
                "position": position,
                "ctr": round(clicks / impressions * 100, 2) if impressions else 0.0,
            }
        )
    return series


def _tracked_series(queries: list[str], *, max_points: int = 30) -> list[dict[str, Any]]:
    """指定クエリの順位推移（スパークライン用）。"""
    from app.services.seo_keywords_store import get_query_series

    out: list[dict[str, Any]] = []
    for q in queries:
        points = [p for p in get_query_series(q) if (p.get("position") or 0) > 0]
        if len(points) < 2:
            continue
        points = points[-max_points:]
        first = float(points[0]["position"])
        last = float(points[-1]["position"])
        out.append(
            {
                "query": q,
                "points": points,
                "current": round(last, 1),
                "delta": round(last - first, 1),
            }
        )
    return out


def get_seo_dashboard_payload() -> dict[str, Any]:
    """管理画面用の集約データ。"""
    from app.services.seo_keywords_config import (
        get_optimizer_settings,
        get_site_meta_keywords,
        get_target_high_value,
        reload_seo_config,
        yaml_path,
    )
    from app.services.seo_keywords_store import load_state

    # 明示リロードはダッシュボード表示時に一度
    try:
        reload_seo_config()
    except Exception:
        pass

    state = load_state()

    # GSC 由来の集計。未設定・データ無しでもダッシュボードは壊さない。
    try:
        from app.services.gsc_client import status as gsc_status

        gsc = gsc_status()
    except Exception as e:
        logger.debug("GSC status 取得失敗: %s", e)
        gsc = {"configured": False, "error": str(e), "site_url": ""}

    try:
        from app.services.seo_opportunities import build_opportunities, summarize

        opportunities = build_opportunities(state.get("performance") or [])
        opportunity_summary = summarize(opportunities)
    except Exception as e:
        logger.warning("機会スコア算出失敗: %s", e)
        opportunities, opportunity_summary = [], {"counts": {}, "labels": {}, "total_potential": 0}

    try:
        totals_series = _totals_series()
        tracked = _tracked_series([o["query"] for o in opportunities[:8]])
    except Exception as e:
        logger.warning("推移データ構築失敗: %s", e)
        totals_series, tracked = [], []

    return {
        "yaml_path": str(yaml_path()),
        "gsc": gsc,
        "gsc_last_sync_at": state.get("gsc_last_sync_at"),
        "opportunities": opportunities,
        "opportunity_summary": opportunity_summary,
        "totals_series": totals_series,
        "tracked_series": tracked,
        "rank_history_days": len(state.get("rank_history") or []),
        "target_high_value": sorted(get_target_high_value()),
        "site_meta_keywords": get_site_meta_keywords(),
        "optimizer": get_optimizer_settings(),
        "auto_boost": state.get("auto_boost") or [],
        "promoted": state.get("promoted") or [],
        "blocked": state.get("blocked") or [],
        "performance": state.get("performance") or [],
        "optimization_log": state.get("optimization_log") or [],
        "health": state.get("health") or {},
        "last_run_at": state.get("last_run_at"),
        "last_candidates": state.get("last_candidates") or [],
    }
