"""有料・ペイウォールが強いメディアのドメイン一覧。

Google News 候補収集・選定保存・記事化の各段階で除外し、
本文が取れず API だけ消費する記事を減らす。
"""
from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 海外の主要ペイウォール紙
_INTERNATIONAL_PAYWALL = frozenset({
    "bloomberg.com",
    "wsj.com",
    "ft.com",
    "nytimes.com",
    "economist.com",
    "washingtonpost.com",
    "thetimes.co.uk",
})

# 日本の有料・会員制が強いメディア（全文スクレイピングがほぼ不可能）
_JP_PAYWALL = frozenset({
    "nikkei.com",          # 日経電子版
    "sankei.com",          # 産経ニュース
    "yomiuri.co.jp",       # 読売新聞オンライン
    "asahi.com",           # 朝日新聞デジタル
    "digital.asahi.com",
    "mainichi.jp",         # 毎日新聞
    "diamond.jp",          # ダイヤモンド・オンライン
    "toyokeizai.net",      # 東洋経済オンライン
    "president.jp",        # PRESIDENT Online
    "gendai.media",        # 現代ビジネス
    "jiji.com",            # 時事通信（会員記事多い）
    "chunichi.co.jp",      # 中日新聞
    "nikkan-gendai.com",   # 日刊現代
})

# 論文は別ルート（paper_rss_fetcher）で取得するためニュース候補からは除外
_PAPER_DOMAINS = frozenset({
    "arxiv.org",
    "pubmed.ncbi.nlm.nih.gov",
    "ncbi.nlm.nih.gov/pubmed",
    "biorxiv.org",
    "medrxiv.org",
    "sciencedaily.com",
    "eurekalert.org",
    "plos.org",
    "frontiersin.org",
    "springer.com/article",
    "wiley.com/doi",
    "cell.com/",
    "science.org/doi",
    "nature.com/articles/",
    "nature.com/nature/articles/",
    "doi.org/",
})

PAYWALL_DOMAINS: frozenset[str] = _INTERNATIONAL_PAYWALL | _JP_PAYWALL


def _normalize_host(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _host_matches_domain(host: str, domain: str) -> bool:
    d = domain.lower().lstrip(".")
    if not host or not d:
        return False
    return host == d or host.endswith("." + d)


def _extra_domains() -> frozenset[str]:
  raw = os.getenv("PAYWALL_EXTRA_DOMAINS", "").strip()
  if not raw:
      return frozenset()
  return frozenset(x.strip().lower() for x in raw.split(",") if x.strip())


def is_paywalled_url(url: str) -> bool:
    """有料・ペイウォール系ドメインなら True。"""
    if not url or not url.startswith("http"):
        return False
    if os.getenv("PAYWALL_BLOCK_ENABLED", "true").strip().lower() in ("0", "false", "no"):
        return False
    host = _normalize_host(url)
    if not host:
        return False
    for domain in PAYWALL_DOMAINS | _extra_domains():
        if _host_matches_domain(host, domain):
            return True
    return False


def is_paper_url(url: str) -> bool:
    """論文系 URL（ニュース候補から除外）。"""
    lower = (url or "").lower()
    return any(marker in lower for marker in _PAPER_DOMAINS)


def is_blocked_news_url(url: str) -> bool:
    """Google News 候補から除外すべき URL（有料紙 + 論文）。"""
    return is_paywalled_url(url) or is_paper_url(url)


def paywall_domain_label(url: str) -> str | None:
    """マッチしたドメイン名を返す（ログ用）。"""
    host = _normalize_host(url)
    for domain in PAYWALL_DOMAINS | _extra_domains():
        if _host_matches_domain(host, domain):
            return domain
    return None
