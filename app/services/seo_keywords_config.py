"""SEO キーワード静的設定（config/seo_keywords.yaml）の読込。"""
from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent.parent
_YAML_PATH = _ROOT / "config" / "seo_keywords.yaml"

_DEFAULTS: dict[str, Any] = {
    "site_meta_keywords": [
        "知リポAI",
        "AIニュース",
        "AI論文",
        "生成AIニュース",
        "AI最新ニュース",
        "論文要約AI",
        "AI解説",
        "人工知能ニュース",
        "チリポ",
        "ちりぽ",
        "AI最新情報",
    ],
    "target_high_value": ["AI", "半導体", "政策", "地震"],
    "low_value_categories": ["スポーツ", "エンタメ"],
    "exclude_title_patterns": (
        r"(号外|訃報|結果|スコア|芸能|ランキング|占い|星座|ゴシップ|"
        r"breaking\s*:?\s*$|score|results|obituary|gossip)"
    ),
    "search_intent_terms": ["とは", "なぜ", "理由", "影響"],
    "comparison_terms": ["比較", "平均", "推移"],
    "question_words": ["何", "とは", "なぜ"],
    "sitemap_high_intent": ["AI", "論文", "研究"],
    "sitemap_low_intent": ["占い", "ゴシップ"],
    "optimizer": {
        "lookback_days": 7,
        "min_article_hits": 2,
        "max_auto_boost": 40,
        "require_trend_or_performance": True,
        "demote_after_days": 14,
    },
}


def yaml_path() -> Path:
    return _YAML_PATH


def reload_seo_config() -> dict[str, Any]:
    """キャッシュを破棄して再読込。"""
    load_seo_config.cache_clear()
    return load_seo_config()


@lru_cache(maxsize=1)
def load_seo_config() -> dict[str, Any]:
    data = dict(_DEFAULTS)
    path = _YAML_PATH
    if not path.exists():
        logger.warning("SEO設定ファイルがありません: %s（デフォルト使用）", path)
        return data
    try:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            logger.warning("SEO設定の形式が不正です: %s", path)
            return data
        for key, default in _DEFAULTS.items():
            if key not in raw:
                continue
            val = raw[key]
            if key == "optimizer" and isinstance(val, dict) and isinstance(default, dict):
                merged = dict(default)
                merged.update(val)
                data[key] = merged
            else:
                data[key] = val
    except Exception as e:
        logger.warning("SEO設定の読込失敗 (%s): %s", path, e)
    return data


def get_target_high_value() -> set[str]:
    cfg = load_seo_config()
    return {str(x).strip() for x in (cfg.get("target_high_value") or []) if str(x).strip()}


def get_manual_promoted() -> set[str]:
    """動的ストアの手動昇格分（循環 import 回避のため遅延）。"""
    try:
        from app.services.seo_keywords_store import get_promoted_keywords

        return set(get_promoted_keywords())
    except Exception:
        return set()


def get_effective_high_value() -> set[str]:
    """固定狙い + 手動昇格 + auto_boost。"""
    base = get_target_high_value() | get_manual_promoted()
    try:
        from app.services.seo_keywords_store import get_auto_boost_keywords

        base |= set(get_auto_boost_keywords())
    except Exception:
        pass
    return base


def get_low_value_categories() -> set[str]:
    cfg = load_seo_config()
    return {str(x).strip() for x in (cfg.get("low_value_categories") or []) if str(x).strip()}


def get_exclude_title_regex() -> re.Pattern:
    cfg = load_seo_config()
    pat = str(cfg.get("exclude_title_patterns") or _DEFAULTS["exclude_title_patterns"])
    # YAML では外側の括弧なしで書いている場合がある
    if not pat.startswith("("):
        pat = f"({pat})"
    try:
        return re.compile(pat, re.IGNORECASE)
    except re.error:
        return re.compile(_DEFAULTS["exclude_title_patterns"], re.IGNORECASE)


def get_search_intent_terms() -> tuple[str, ...]:
    cfg = load_seo_config()
    return tuple(str(x) for x in (cfg.get("search_intent_terms") or []) if str(x).strip())


def get_comparison_terms() -> tuple[str, ...]:
    cfg = load_seo_config()
    return tuple(str(x) for x in (cfg.get("comparison_terms") or []) if str(x).strip())


def get_question_words() -> list[str]:
    cfg = load_seo_config()
    return [str(x) for x in (cfg.get("question_words") or []) if str(x).strip()]


def get_sitemap_high_intent() -> tuple[str, ...]:
    cfg = load_seo_config()
    return tuple(str(x) for x in (cfg.get("sitemap_high_intent") or []) if str(x).strip())


def get_sitemap_low_intent() -> tuple[str, ...]:
    cfg = load_seo_config()
    return tuple(str(x) for x in (cfg.get("sitemap_low_intent") or []) if str(x).strip())


def get_site_meta_keywords() -> str:
    cfg = load_seo_config()
    kws = [str(x).strip() for x in (cfg.get("site_meta_keywords") or []) if str(x).strip()]
    return ",".join(kws)


def get_optimizer_settings() -> dict[str, Any]:
    cfg = load_seo_config()
    opt = cfg.get("optimizer") or {}
    if not isinstance(opt, dict):
        opt = {}
    base = dict(_DEFAULTS["optimizer"])
    base.update(opt)
    return base
