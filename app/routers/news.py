"""ニュース関連ルート"""
import json
import logging
import re
import uuid
from datetime import datetime
from difflib import SequenceMatcher
import threading
import unicodedata
from urllib.parse import urlencode

from fastapi import APIRouter, Request, HTTPException, Form, Header
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app.config import settings, is_rss_and_ai_disabled
from app.services.news_aggregator import NewsAggregator
from app.services.rss_service import NewsItem, sanitize_display_text
from app.services.article_cache import save_article
from app.services.ai_batch_service import generate_all_explanations, upgrade_personas_with_claude_if_configured
from app.services.explanation_cache import save_cache
from app.services.ai_service import (
    explain_article_with_ai,
    get_image_url,
    PERSONAS,
)
from app.services.image_assets import is_placeholder_image

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
logger = logging.getLogger(__name__)

SITE_JSONLD_TITLE = "知リポAI — 偉人AIが解説するAI論文・ニュースメディア"
SITE_JSONLD_DESCRIPTION = (
    "知リポAIは、ブッダ・ニーチェ・アインシュタインなど歴史的偉人AIが"
    "最新のAI論文・国内外ニュースを日本語でわかりやすく解説する、無料のニュースメディアです。"
)

# カテゴリハブページの定義（slug → メタ情報）
CATEGORY_PAGES: dict[str, dict] = {
    "ai": {
        "label": "AI論文・研究",
        "category": "研究・論文",
        "title": "AI論文・研究解説ニュース一覧 | 知リポAI",
        "desc": "arXiv・Nature・Science最新AI論文を偉人AIが日本語でわかりやすく解説。ChatGPT・LLM・生成AI・深層学習の最新研究を毎日更新。",
        "h1": "🔬 AI論文・研究解説",
        "keywords": "AI論文,研究論文解説,arXiv日本語,LLM論文,生成AI論文,機械学習論文,深層学習論文",
        "hero_desc": "arXiv・Nature・Scienceなどの最新AI研究論文を偉人AIが毎日解説。難しい論文も「1分で理解」できます。",
    },
    "tech": {
        "label": "テクノロジー",
        "category": "テクノロジー",
        "title": "テクノロジーニュース AI解説一覧 | 知リポAI",
        "desc": "最新テクノロジーニュースを偉人AIが解説。スマートフォン・半導体・IT・宇宙開発・自動運転など最新テクノロジー情報を毎日更新。",
        "h1": "💡 テクノロジーニュース",
        "keywords": "テクノロジーニュース,最新テクノロジー,IT最新情報,半導体ニュース,宇宙開発ニュース",
        "hero_desc": "半導体・IT・宇宙開発・AI製品など最新テクノロジーニュースを偉人AIがわかりやすく解説します。",
    },
    "science": {
        "label": "科学",
        "category": "科学",
        "title": "科学ニュース AI解説一覧 | 知リポAI",
        "desc": "最新科学ニュースを偉人AIが解説。宇宙科学・生命科学・物理・化学など科学の最新発見をわかりやすく日本語で解説します。",
        "h1": "🔭 科学ニュース",
        "keywords": "科学ニュース,最新科学,科学解説,宇宙ニュース,生命科学ニュース,物理学ニュース",
        "hero_desc": "宇宙・生命科学・物理・化学など最新科学ニュースを偉人AIがわかりやすく解説します。",
    },
    "world": {
        "label": "国際ニュース",
        "category": "国際",
        "title": "国際ニュース AI解説一覧 | 知リポAI",
        "desc": "世界の最新ニュースを偉人AIが解説。アメリカ・中国・ヨーロッパ・中東など国際情勢・外交・安全保障ニュースをわかりやすく解説。",
        "h1": "🌍 国際ニュース",
        "keywords": "国際ニュース,世界のニュース,海外ニュース最新,外交ニュース,国際情勢",
        "hero_desc": "アメリカ・中国・ヨーロッパ・中東など世界の最新ニュースを偉人AIが解説。複数の視点で読み解きます。",
    },
    "social": {
        "label": "社会・経済",
        "category": "社会・経済",
        "title": "社会・経済ニュース AI解説一覧 | 知リポAI",
        "desc": "日本の社会・経済の最新ニュースを偉人AIが解説。株式・為替・政治・社会問題・企業ニュースなど最新情報をわかりやすく解説。",
        "h1": "📊 社会・経済ニュース",
        "keywords": "経済ニュース,社会ニュース,株式ニュース,日本経済最新,政治ニュース",
        "hero_desc": "日本の最新経済・社会ニュースを偉人AIが背景・影響まで丁寧に解説します。",
    },
    "sports": {
        "label": "スポーツ",
        "category": "スポーツ",
        "title": "スポーツニュース AI解説一覧 | 知リポAI",
        "desc": "最新スポーツニュースを偉人AIが解説。野球・サッカー・オリンピックなどスポーツの最新情報をわかりやすく解説します。",
        "h1": "⚽ スポーツニュース",
        "keywords": "スポーツニュース,野球ニュース,サッカーニュース,スポーツ最新情報",
        "hero_desc": "野球・サッカー・テニスなど最新スポーツニュースを偉人AIが解説します。",
    },
    "entertainment": {
        "label": "エンタメ",
        "category": "エンタメ",
        "title": "エンタメニュース AI解説一覧 | 知リポAI",
        "desc": "最新エンタメニュースを偉人AIが解説。映画・音楽・ゲーム・アニメなどエンタメの最新情報をわかりやすく解説します。",
        "h1": "🎬 エンタメニュース",
        "keywords": "エンタメニュース,映画ニュース,音楽ニュース,ゲームニュース,アニメニュース",
        "hero_desc": "映画・音楽・ゲーム・アニメなど最新エンタメニュースを偉人AIが解説します。",
    },
}


import re as _re


def slugify_title(title: str, max_len: int = 55) -> str:
    """記事タイトルからSEOフレンドリーなURLスラッグを生成。日本語はそのまま保持。"""
    s = (title or "").strip()
    # URLに不向きな記号を除去
    s = _re.sub(r'[「」『』【】〈〉《》\[\]{}()（）<>""\'\'`！!？?。、，,．\.。:;：；・＊*＋+＝=＆&＠@＃#｜|＼\\／/]', '', s)
    # 空白・全角スペース → ハイフン
    s = _re.sub(r'[\s\u3000　]+', '-', s)
    # 連続ハイフンを1つに
    s = _re.sub(r'-+', '-', s)
    s = s.strip('-')
    return s[:max_len] if s else ""


def article_url_path(article) -> str:
    """記事オブジェクトからSEOフレンドリーなURLパス（/topic/...）を生成。
    タイトルが取れればスラッグ、なければ元のIDをそのまま使う。"""
    if isinstance(article, dict):
        article_id = str(article.get("id") or "")
        title = str(article.get("title") or "")
    else:
        article_id = getattr(article, 'id', '') or ''
        title = getattr(article, 'title', '') or ''
    slug = slugify_title(title)
    if slug:
        # ユニーク性確保のため末尾にIDの後ろ6文字を付与
        suffix = article_id[-6:] if len(article_id) >= 6 else article_id
        return f"/topic/{slug}-{suffix}" if suffix else f"/topic/{slug}"
    return f"/topic/{article_id}"


def _find_article_by_slug(slug: str):
    """スラッグ文字列から記事オブジェクトを返す。見つからなければ None。"""
    all_news = NewsAggregator.get_news()
    for a in all_news:
        path = article_url_path(a)
        if path == f"/topic/{slug}":
            return a
    return None


def _public_html_cache_headers() -> dict[str, str]:
    """CDN・ブラウザ向け Cache-Control。PUBLIC_HTML_CACHE_MAX_AGE_SEC<=0 なら空（ヘッダ付与しない）。"""
    try:
        sec = int(getattr(settings, "PUBLIC_HTML_CACHE_MAX_AGE_SEC", 0) or 0)
    except Exception:
        sec = 0
    if sec <= 0:
        return {}
    return {"Cache-Control": f"public, max-age={sec}"}

# 一覧ジャンルタブの data-cat と一致させる（DB/RSS の category がタブ6種以外だと「すべて」以外で消える）
_NEWS_TAB_CATEGORY_SET = frozenset({"国内", "国際", "テクノロジー", "政治・社会", "スポーツ", "エンタメ"})
_NEWS_TAB_OTHER = "その他"

# RSS の MD5 16hex / 手動 manual- / curated cc- / スラッグ（日本語+ハイフン+英数字）。
# スラッグURLは article_url_path() が生成する形式（タイトル-suffix6文字）
_TOPIC_ID_PLAUSIBLE = re.compile(
    r"^(?:[0-9a-f]{16}|manual-[0-9a-f]{8,40}|cc-[0-9a-f]{12,20}|dg-[0-9a-f]{12,20}|gn-[0-9a-f]{12,20}|.{2,80})$"
)


def _topic_id_plausible(topic_id: str) -> bool:
    t = (topic_id or "").strip()
    if not t or len(t) > 120:
        return False
    # 明らかに不正なパターンは弾く（パストラバーサル等）
    if ".." in t or "/" in t or "\\" in t or "\x00" in t:
        return False
    return True


_BOT_UA_KEYWORDS = (
    "bot",
    "spider",
    "crawler",
    "scrapy",
    "facebookexternalhit",
    "slackbot",
    "discordbot",
    "telegrambot",
    "curl",
    "wget",
    "python-requests",
    "go-http-client",
    "httpclient",
    "okhttp",
    "zgrab",
    "masscan",
    "nmap",
)

_BOT_UA_ALLOWLIST = (
    # 検索エンジン（SEO上ブロックしたくない）
    "googlebot",
    "bingbot",
    "duckduckbot",
    "yandexbot",
    "baiduspider",
)


def _is_probably_bot_request(request: Request) -> bool:
    """User-Agent などから「人間の閲覧ではなさそう」なリクエストを雑に判定する。

    目的: /topic/{id} を総当たりするクローラーが DB を起こすのを抑える（メモリに無いIDはDBを見ない）。
    """
    try:
        ua = (request.headers.get("user-agent") or "").strip().lower()
    except Exception:
        ua = ""
    if not ua:
        return True
    if any(k in ua for k in _BOT_UA_ALLOWLIST):
        return False
    return any(k in ua for k in _BOT_UA_KEYWORDS)


def _news_tab_filter_category(item: NewsItem) -> str:
    """タブ用ジャンル。未登録・空は「その他」（item.category 本体は変更しない）。"""
    c = (getattr(item, "category", None) or "").strip()
    if c in _NEWS_TAB_CATEGORY_SET:
        return c
    return _NEWS_TAB_OTHER


def _news_tab_category_order_for(by_cat: dict) -> list[str]:
    from app.services.news_aggregator import CATEGORY_ORDER

    base = [c for c in CATEGORY_ORDER if c != "研究・論文"]
    if by_cat.get(_NEWS_TAB_OTHER):
        return list(base) + [_NEWS_TAB_OTHER]
    return base

PERSONA_IMAGE_MAP = {
    "ブッダ":       "/static/char-imgs/ブッダ.png",
    "織田信長":     "/static/char-imgs/織田信長.png",
    "吉田松陰":     "/static/char-imgs/吉田松陰.png",
    "坂本龍馬":     "/static/char-imgs/坂本龍馬.png",
    "太宰治":       "/static/char-imgs/太宰治.png",
    "葛飾北斎":     "/static/char-imgs/葛飾北斎.png",
    "ソクラテス":   "/static/char-imgs/ソクラテス.png",
    "野口英世":     "/static/char-imgs/野口英世.png",
    "ダヴィンチ":   "/static/char-imgs/ダヴィンチ.png",
    "エジソン":     "/static/char-imgs/エジソン.png",
    "アインシュタイン": "/static/char-imgs/アインシュタイン.png",
    "ナイチンゲール":   "/static/char-imgs/ナイチンゲール.png",
    "ガリレオ":     "/static/char-imgs/ガリレオ.png",
    "ニーチェ":     "/static/char-imgs/ニーチェ.png",
    "ミドルマン":   "/static/char-imgs/ミドルマン.png",
}

# 14偉人の Person JSON-LD 用データ（sameAs / 生没年 / 職業）
_PERSONA_WIKI_DATA: dict[str, dict] = {
    "ブッダ": {
        "sameAs": [
            "https://ja.wikipedia.org/wiki/ゴータマ・ブッダ",
            "https://en.wikipedia.org/wiki/Gautama_Buddha",
            "https://www.wikidata.org/wiki/Q9441",
        ],
        "birthDate": "-0563",
        "deathDate": "-0483",
        "jobTitle": "宗教家・哲学者",
        "nationality": "IN",
    },
    "織田信長": {
        "sameAs": [
            "https://ja.wikipedia.org/wiki/織田信長",
            "https://en.wikipedia.org/wiki/Oda_Nobunaga",
            "https://www.wikidata.org/wiki/Q46616",
        ],
        "birthDate": "1534-06-23",
        "deathDate": "1582-06-21",
        "jobTitle": "戦国武将・大名",
        "nationality": "JP",
    },
    "吉田松陰": {
        "sameAs": [
            "https://ja.wikipedia.org/wiki/吉田松陰",
            "https://en.wikipedia.org/wiki/Yoshida_Sh%C5%8Din",
            "https://www.wikidata.org/wiki/Q312534",
        ],
        "birthDate": "1830-09-20",
        "deathDate": "1859-10-27",
        "jobTitle": "思想家・教育者",
        "nationality": "JP",
    },
    "坂本龍馬": {
        "sameAs": [
            "https://ja.wikipedia.org/wiki/坂本龍馬",
            "https://en.wikipedia.org/wiki/Sakamoto_Ry%C5%8Dma",
            "https://www.wikidata.org/wiki/Q188415",
        ],
        "birthDate": "1836-01-03",
        "deathDate": "1867-12-10",
        "jobTitle": "志士・政治活動家",
        "nationality": "JP",
    },
    "太宰治": {
        "sameAs": [
            "https://ja.wikipedia.org/wiki/太宰治",
            "https://en.wikipedia.org/wiki/Osamu_Dazai",
            "https://www.wikidata.org/wiki/Q130760",
        ],
        "birthDate": "1909-06-19",
        "deathDate": "1948-06-13",
        "jobTitle": "小説家",
        "nationality": "JP",
    },
    "葛飾北斎": {
        "sameAs": [
            "https://ja.wikipedia.org/wiki/葛飾北斎",
            "https://en.wikipedia.org/wiki/Hokusai",
            "https://www.wikidata.org/wiki/Q5586",
        ],
        "birthDate": "1760-10-31",
        "deathDate": "1849-05-10",
        "jobTitle": "浮世絵師・画家",
        "nationality": "JP",
    },
    "ソクラテス": {
        "sameAs": [
            "https://ja.wikipedia.org/wiki/ソクラテス",
            "https://en.wikipedia.org/wiki/Socrates",
            "https://www.wikidata.org/wiki/Q913",
        ],
        "birthDate": "-0470",
        "deathDate": "-0399",
        "jobTitle": "哲学者",
        "nationality": "GR",
    },
    "野口英世": {
        "sameAs": [
            "https://ja.wikipedia.org/wiki/野口英世",
            "https://en.wikipedia.org/wiki/Hideyo_Noguchi",
            "https://www.wikidata.org/wiki/Q190858",
        ],
        "birthDate": "1876-11-09",
        "deathDate": "1928-05-21",
        "jobTitle": "細菌学者・医学者",
        "nationality": "JP",
    },
    "ダヴィンチ": {
        "sameAs": [
            "https://ja.wikipedia.org/wiki/レオナルド・ダ・ヴィンチ",
            "https://en.wikipedia.org/wiki/Leonardo_da_Vinci",
            "https://www.wikidata.org/wiki/Q762",
        ],
        "birthDate": "1452-04-15",
        "deathDate": "1519-05-02",
        "jobTitle": "芸術家・科学者・発明家",
        "nationality": "IT",
    },
    "エジソン": {
        "sameAs": [
            "https://ja.wikipedia.org/wiki/トーマス・エジソン",
            "https://en.wikipedia.org/wiki/Thomas_Edison",
            "https://www.wikidata.org/wiki/Q8743",
        ],
        "birthDate": "1847-02-11",
        "deathDate": "1931-10-18",
        "jobTitle": "発明家・実業家",
        "nationality": "US",
    },
    "アインシュタイン": {
        "sameAs": [
            "https://ja.wikipedia.org/wiki/アルベルト・アインシュタイン",
            "https://en.wikipedia.org/wiki/Albert_Einstein",
            "https://www.wikidata.org/wiki/Q937",
        ],
        "birthDate": "1879-03-14",
        "deathDate": "1955-04-18",
        "jobTitle": "物理学者",
        "nationality": "DE",
    },
    "ナイチンゲール": {
        "sameAs": [
            "https://ja.wikipedia.org/wiki/フローレンス・ナイチンゲール",
            "https://en.wikipedia.org/wiki/Florence_Nightingale",
            "https://www.wikidata.org/wiki/Q34517",
        ],
        "birthDate": "1820-05-12",
        "deathDate": "1910-08-13",
        "jobTitle": "看護師・統計学者・社会改革家",
        "nationality": "GB",
    },
    "ガリレオ": {
        "sameAs": [
            "https://ja.wikipedia.org/wiki/ガリレオ・ガリレイ",
            "https://en.wikipedia.org/wiki/Galileo_Galilei",
            "https://www.wikidata.org/wiki/Q307",
        ],
        "birthDate": "1564-02-15",
        "deathDate": "1642-01-08",
        "jobTitle": "天文学者・物理学者・数学者",
        "nationality": "IT",
    },
    "ニーチェ": {
        "sameAs": [
            "https://ja.wikipedia.org/wiki/フリードリヒ・ニーチェ",
            "https://en.wikipedia.org/wiki/Friedrich_Nietzsche",
            "https://www.wikidata.org/wiki/Q9358",
        ],
        "birthDate": "1844-10-15",
        "deathDate": "1900-08-25",
        "jobTitle": "哲学者・文献学者",
        "nationality": "DE",
    },
}


def _persona_image_url(name: str | None) -> str:
    return PERSONA_IMAGE_MAP.get((name or "").strip(), "/static/site-imgs/ロゴ.png")


def _persona_display_comment(raw: str) -> str:
    """persona opinion 文字列を表示用テキストに変換。
    新形式はプレーン文字列。旧JSON形式（{"comment"} / {"short","body"}）も救済する。"""
    s = (raw or "").strip()
    if s.startswith("{"):
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                return str(
                    obj.get("comment") or obj.get("short") or obj.get("body") or ""
                ).strip()
        except Exception:
            pass
    return s


def _get_featured_items_with_persona(news_list: list, max_items: int = 3) -> list[dict]:
    """解説つき記事から話題記事+偉人コメントを最大 max_items 件取得する。"""
    from app.services.explanation_cache import get_cached
    results = []
    for item in news_list[:30]:
        if len(results) >= max_items:
            break
        try:
            cached = get_cached(item.id)
        except Exception:
            continue
        if not cached:
            continue
        cached_personas = cached.get("personas") or []
        display_ids = cached.get("display_persona_ids") or []
        persona_id = None
        comment = ""
        if isinstance(display_ids, list) and display_ids and \
                isinstance(cached_personas, list) and len(cached_personas) == 3:
            try:
                persona_id = int(display_ids[0])
                comment = _persona_display_comment(str(cached_personas[0])) if cached_personas[0] else ""
            except Exception:
                pass
        elif isinstance(cached_personas, list):
            for pid, c in enumerate(cached_personas):
                if c and 0 <= pid < len(PERSONAS):
                    persona_id = pid
                    comment = _persona_display_comment(str(c))
                    break
        if persona_id is None or not (0 <= persona_id < len(PERSONAS)):
            continue
        p = PERSONAS[persona_id]
        img = item.image_url
        if not img:
            img = get_image_url(item.id, 800, 450)
        results.append({
            "id": item.id,
            "title": item.title,
            "source": item.source or "",
            "image_url": img,
            "persona_id": persona_id,
            "persona_name": p["name"],
            "persona_emoji": p.get("emoji", ""),
            "persona_image_url": _persona_image_url(p["name"]),
            "persona_comment": comment,
        })
    return results


def _get_latest_consultations(limit: int = 3) -> list[dict]:
    try:
        from app.services.consultation_store import get_consultations
        return get_consultations(limit=limit)
    except Exception:
        return []


def _build_persona_person_node(p: dict, site_url: str) -> dict:
    """偉人1人分の Person JSON-LD ノード（sameAs / 生没年付き）。"""
    name = str(p.get("name", "") or "")
    persona_id = p.get("id", "")
    bio = str(p.get("bio", "") or "").replace("\n", " ").strip()
    wiki = _PERSONA_WIKI_DATA.get(name, {})
    base = site_url.rstrip("/")
    node: dict = {
        "@type": "Person",
        "@id": f"{base}/personas/{persona_id}",
        "name": name,
        "url": f"{base}/personas/{persona_id}",
        "image": f"{base}{_persona_image_url(name)}",
    }
    if bio:
        node["description"] = bio[:200]
    if wiki.get("jobTitle"):
        node["jobTitle"] = wiki["jobTitle"]
    if wiki.get("birthDate"):
        node["birthDate"] = wiki["birthDate"]
    if wiki.get("deathDate"):
        node["deathDate"] = wiki["deathDate"]
    if wiki.get("sameAs"):
        node["sameAs"] = wiki["sameAs"]
    return node


def _build_persona_view(p: dict) -> dict:
    role = str(p.get("role", "") or "")
    summary = role.split("。")[0].replace("あなたは", "").replace("である", "").strip()
    thought = ""
    style = ""
    advice = "最後に実行可能な提案・アドバイスを必ず添える"
    m_thought = re.search(r"思考プロセス（必須）:\s*(.+?)。", role)
    if m_thought:
        thought = m_thought.group(1).strip()
    m_style = re.search(r"文体は(.+?)。", role)
    if m_style:
        style = m_style.group(1).strip()
    return {
        "id": p.get("id"),
        "name": p.get("name"),
        "emoji": p.get("emoji"),
        "type": "論理型" if p.get("type") == "logic" else "エンタメ型",
        "summary": summary,
        "thought_process": thought,
        "style": style,
        "advice_rule": advice,
        "prompt_text": role,
        "image_url": _persona_image_url(p.get("name")),
        "bio": str(p.get("bio", "") or "").strip(),
        "catchphrase": list(p.get("catchphrase") or []),
        "admire": str(p.get("admire", "") or "").strip(),
    }


def _json_safe_for_template(obj):
    """Jinja の |tojson が扱えない型（Decimal, DatetimeWithNanoseconds 等）を落とさないよう正規化。"""
    if obj is None:
        return None
    try:
        return json.loads(json.dumps(obj, ensure_ascii=False, default=str))
    except Exception:
        return None


def _coerce_mapping(val):
    """キャッシュ・Firestore 由来の値を dict に（JSON文字列も許容）。"""
    if val is None:
        return None
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        try:
            out = json.loads(s)
            return out if isinstance(out, dict) else None
        except Exception:
            return None
    return None


def _normalize_quiz_options(raw) -> list[dict[str, str]]:
    """過去データ互換: options が [{id,label}] でも文字列配列でも受け付ける"""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for opt in raw:
        if isinstance(opt, dict):
            oid = opt.get("id")
            lab = opt.get("label")
            oid_s = "" if oid is None else str(oid).strip()
            lab_s = "" if lab is None else str(lab).strip()
            if lab_s or oid_s:
                out.append({"id": oid_s, "label": lab_s or oid_s})
        elif opt is not None:
            s = str(opt).strip()
            if s:
                out.append({"id": s, "label": s})
    return out


def _normalize_quiz_payload(raw):
    """
    vote_data / paper_quiz のフィールド名ゆらぎを吸収して共通化する。
    例:
      - question / quiz_question / title
      - options / choices
      - answer_id / answer / correct_answer / correct_option
      - explanation / reason / rationale / answer_explanation
    """
    d = _coerce_mapping(raw)
    if not d:
        return None
    d = _json_safe_for_template(d)
    if not isinstance(d, dict):
        return None

    def _pick(*keys):
        for k in keys:
            if k in d and d.get(k) not in (None, ""):
                return d.get(k)
        return ""

    options = _normalize_quiz_options(_pick("options", "choices", "quiz_options"))
    question = _pick("question", "quiz_question", "title")
    answer_id = _pick("answer_id", "answer", "correct_answer", "correct_option")
    explanation = _pick("explanation", "reason", "rationale", "answer_explanation")
    learning_point = _pick("learning_point", "point", "takeaway")
    key_term = _pick("key_term", "term", "keyword")
    key_term_note = _pick("key_term_note", "term_note", "keyword_note")

    # "A"/"B"/"C"/"D" 形式や "1" 形式も可能な範囲で合わせる
    aid = str(answer_id).strip().lower()
    if aid in {"1", "2", "3", "4"}:
        answer_id = ["a", "b", "c", "d"][int(aid) - 1]
    elif aid in {"a", "b", "c", "d"}:
        answer_id = aid
    else:
        answer_id = str(answer_id).strip()

    return {
        "question": "" if question is None else str(question),
        "options": options,
        "answer_id": "" if answer_id is None else str(answer_id),
        "explanation": "" if explanation is None else str(explanation),
        "learning_point": "" if learning_point is None else str(learning_point),
        "key_term": "" if key_term is None else str(key_term),
        "key_term_note": "" if key_term_note is None else str(key_term_note),
    }


def _sanitize_quick_understand_for_page(val):
    d = _coerce_mapping(val)
    if not d:
        return None
    d = _json_safe_for_template(d)
    if not isinstance(d, dict):
        return None
    for k in ("what", "why", "how"):
        v = d.get(k)
        if isinstance(v, str):
            d[k] = v.strip()
        elif v is None:
            d[k] = ""
        else:
            d[k] = str(v).strip()
    return d


def _sanitize_vote_data_for_page(val):
    d = _normalize_quiz_payload(val)
    if not d:
        return None
    opts = _normalize_quiz_options(d.get("options"))
    if not opts:
        return None
    d["options"] = opts
    # Jinja: キー欠落だと vote_data.answer_id が Undefined になり |tojson で落ちるため必ず文字列化
    for _k in ("answer_id", "explanation", "learning_point", "key_term", "key_term_note", "question"):
        v = d.get(_k)
        d[_k] = "" if v is None else str(v)
    return d


def _sanitize_paper_graph_for_page(val):
    d = _coerce_mapping(val)
    if not d:
        return {}
    d = _json_safe_for_template(d)
    if not isinstance(d, dict):
        return {}
    tags = d.get("related_tags")
    if not isinstance(tags, list):
        d["related_tags"] = []
    tm = d.get("timeline_message")
    if tm is not None and not isinstance(tm, str):
        d["timeline_message"] = str(tm)
    return d


def _sanitize_paper_quiz_for_page(val):
    d = _normalize_quiz_payload(val)
    if not d:
        return {}
    options = _normalize_quiz_options(d.get("options"))
    # 既存キャッシュ互換: 過去の3択データは4択表示に補完する
    # （answer_id はそのまま有効。追加肢は「該当なし」に固定）
    existing_ids = {str(o.get("id", "")).strip() for o in options}
    if len(options) == 3:
        for cand in ("d", "4", "none"):
            if cand not in existing_ids:
                options.append({"id": cand, "label": "該当なし"})
                break
    d["options"] = options
    for _k in ("answer_id", "explanation", "question", "learning_point", "key_term", "key_term_note"):
        v = d.get(_k)
        d[_k] = "" if v is None else str(v)
    return d


def _sanitize_deep_insights_for_page(val):
    d = _coerce_mapping(val)
    if not d:
        return None
    d = _json_safe_for_template(d)
    if not isinstance(d, dict):
        return None
    mer = d.get("merits")
    if mer is None:
        mer = []
    elif not isinstance(mer, list):
        mer = [str(mer).strip()] if str(mer).strip() else []
    else:
        mer = [str(x).strip() for x in mer if x is not None and str(x).strip()]
    risk = d.get("risks")
    if risk is None:
        risk = []
    elif not isinstance(risk, list):
        risk = [str(risk).strip()] if str(risk).strip() else []
    else:
        risk = [str(x).strip() for x in risk if x is not None and str(x).strip()]
    fp = d.get("future_prediction")
    if fp is None:
        fp = ""
    elif not isinstance(fp, str):
        fp = str(fp)
    out = {"merits": mer, "risks": risk, "future_prediction": fp}
    if not mer and not risk and not fp.strip():
        return None
    return out

# /papers 一覧で何度も叩かれやすい「関連タグ」をメモリキャッシュ
# （記事は基本的に静的なので、DB/Firestore 読みを抑える目的）
_PAPER_RELATED_TAGS_CACHE: dict[str, list[str]] = {}
_PAPER_RELATED_TAGS_LOCK = threading.Lock()


@router.get("/robots.txt")
async def robots_txt(request: Request):
    """検索エンジン・AIクローラー向け robots.txt"""
    site_url = _get_site_url(request)
    ai_bots = (
        "GPTBot",
        "Claude-Web",
        "Google-Extended",
        "PerplexityBot",
        "Amazonbot",
    )
    ai_sections = "".join(f"User-agent: {bot}\nAllow: /\n\n" for bot in ai_bots)
    body = (
        f"User-agent: *\n"
        f"Allow: /\n"
        f"Disallow: /admin\n"
        f"Disallow: /confirm\n"
        f"Disallow: /saved\n"
        f"Disallow: /?keyword=\n"
        f"Disallow: /news?keyword=\n\n"
        f"Sitemap: {site_url}/sitemap-index.xml\n"
        f"Sitemap: {site_url}/sitemap.xml\n"
        f"Sitemap: {site_url}/sitemap-news.xml\n\n"
        f"RSS: {site_url}/feed.xml\n\n"
        f"{ai_sections}"
    )
    return Response(content=body, media_type="text/plain; charset=utf-8")


@router.get("/llms.txt")
async def llms_txt(request: Request):
    """AIエージェント向け llms.txt（GEO: Generative Engine Optimization）"""
    site_url = _get_site_url(request)
    body = f"""# 知リポAI

> 歴史的偉人AIが最新研究論文・ニュースを多角的に解説する知的ニュースメディア（日本語）。

## サイト概要

知リポAI（チリポAI）は、ブッダ・ニーチェ・織田信長・アインシュタインなど歴史的偉人AIが、最新のAI論文・科学論文・国内外ニュースを解説するサービスです。arXiv・Nature・Science・NHK・BBC・Reutersなどから毎日収集し、「1分で理解」「詳しく読む」の2段階で解説します。

## 主要ページ

- [AI論文解説（トップ）]({site_url}/): 最新研究論文をAIが平易に解説。arXiv・Nature・Science等から毎日収集。
- [AIニュースアーカイブ]({site_url}/news): 国内外ニュースのAI解説アーカイブ。カテゴリ別フィルター対応。
- [AI投票・政策]({site_url}/ai): AIが生成する政策提案とユーザー投票。少子化・経済等のテーマ。
- [キャラクター一覧]({site_url}/personas): 解説キャラクター（偉人AI）の一覧とプロフィール。

## コンテンツ仕様

- **ソース**: arXiv（cs.AI/cs.LG/cs.CL/cs.CV/cs.RO等）、Nature、Science、BMJ Open、PLOS ONE、NHK、BBC、Reuters、AP News 他
- **更新頻度**: 8:30 / 13:00 / 16:30 / 19:00 / 22:00（JST）毎日
- **解説言語**: 日本語
- **一次ソース**: 各記事ページに元論文・ニュースへのリンクを掲載（arXiv・Nature・DOI等）

## AIキャラクター（偉人）

ブッダ、織田信長、吉田松陰、坂本龍馬、太宰治、葛飾北斎、ソクラテス、野口英世、ダヴィンチ、エジソン、アインシュタイン、ナイチンゲール、ガリレオ、ニーチェ

## 免責事項

当サイトの解説はAI生成です。医療・法律・投資判断の根拠には使用しないでください。各記事の一次ソース（arXiv・Nature等）を必ずご確認ください。

## クロール情報

- robots.txt: {site_url}/robots.txt
- sitemap: {site_url}/sitemap.xml
- sitemap-news（直近48時間）: {site_url}/sitemap-news.xml
"""
    return Response(content=body, media_type="text/plain; charset=utf-8")


@router.get("/{key_filename}.txt", include_in_schema=False)
async def indexnow_key_file(key_filename: str):
    """IndexNow キー検証用（https://host/{INDEXNOW_KEY}.txt にキー文字列を返す）。"""
    from app.services.indexnow_service import indexnow_key

    key = indexnow_key()
    if not key or key_filename != key:
        raise HTTPException(status_code=404, detail="Not Found")
    return Response(content=key + "\n", media_type="text/plain; charset=utf-8")


@router.get("/sitemap-index.xml")
async def sitemap_index_xml(request: Request):
    """Parent sitemap for Search Console and Bing Webmaster Tools."""
    from html import escape as _xml_escape

    site_url = _get_site_url(request).rstrip("/")
    today = datetime.now().date().isoformat()
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        f"  <sitemap><loc>{_xml_escape(site_url + '/sitemap.xml', quote=True)}</loc><lastmod>{today}</lastmod></sitemap>",
        f"  <sitemap><loc>{_xml_escape(site_url + '/sitemap-news.xml', quote=True)}</loc><lastmod>{today}</lastmod></sitemap>",
        "</sitemapindex>",
    ]
    return Response(content="\n".join(lines), media_type="application/xml; charset=utf-8")


def _latest_articles_for_feed(limit: int = 50) -> list:
    articles = list(getattr(NewsAggregator, "_news_cache", []) or [])
    if not articles:
        try:
            NewsAggregator.sync_list_cache_from_db(force=False)
        except Exception:
            pass
        articles = list(getattr(NewsAggregator, "_news_cache", []) or [])
    return articles[:limit]


def _feed_pubdate(article=None) -> str:
    from datetime import timezone
    from email.utils import format_datetime

    dt = getattr(article, "added_at", None) or getattr(article, "published", None) if article else None
    if dt and hasattr(dt, "tzinfo"):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return format_datetime(dt)
    return format_datetime(datetime.now(timezone.utc))


@router.get("/feed.xml")
async def feed_xml(request: Request):
    """Latest article RSS feed for crawlers and subscribers."""
    from html import escape as _xml_escape

    site_url = _get_site_url(request).rstrip("/")
    articles = _latest_articles_for_feed()
    channel_date = _feed_pubdate(articles[0]) if articles else _feed_pubdate()
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "  <channel>",
        f"    <title>{_xml_escape(SITE_JSONLD_TITLE, quote=True)}</title>",
        f"    <link>{_xml_escape(site_url + '/news', quote=True)}</link>",
        f"    <description>{_xml_escape(SITE_JSONLD_DESCRIPTION, quote=True)}</description>",
        "    <language>ja</language>",
        f"    <lastBuildDate>{_xml_escape(channel_date, quote=True)}</lastBuildDate>",
        f"    <atom:link href=\"{_xml_escape(site_url + '/feed.xml', quote=True)}\" rel=\"self\" type=\"application/rss+xml\" />",
    ]
    for article in articles:
        link = f"{site_url}{article_url_path(article)}"
        title = str(getattr(article, "title", "") or "").strip()
        summary = str(getattr(article, "summary", "") or "").strip()
        source = str(getattr(article, "source", "") or "").strip()
        category = str(getattr(article, "category", "") or "").strip()
        lines.extend([
            "    <item>",
            f"      <title>{_xml_escape(title, quote=True)}</title>",
            f"      <link>{_xml_escape(link, quote=True)}</link>",
            f"      <guid isPermaLink=\"true\">{_xml_escape(link, quote=True)}</guid>",
            f"      <description>{_xml_escape(summary[:600], quote=True)}</description>",
            f"      <pubDate>{_xml_escape(_feed_pubdate(article), quote=True)}</pubDate>",
        ])
        if category:
            lines.append(f"      <category>{_xml_escape(category, quote=True)}</category>")
        if source:
            lines.append(f"      <source url=\"{_xml_escape(link, quote=True)}\">{_xml_escape(source, quote=True)}</source>")
        lines.append("    </item>")
    lines.extend(["  </channel>", "</rss>"])
    return Response(content="\n".join(lines), media_type="application/rss+xml; charset=utf-8")


@router.get("/rss.xml")
async def rss_xml(request: Request):
    return await feed_xml(request)


@router.get("/sitemap.xml")
async def sitemap_xml(request: Request):
    """SEO用 sitemap.xml。

    常にメモリキャッシュ（_news_cache）から生成する。古い data/sitemap.xml をそのまま返さない。
    キャッシュが空のときだけ DB 同期を試し、それでも無ければスナップショット／静的URLへフォールバック。
    """
    from app.services.sitemap_service import read_sitemap_snapshot, render_sitemap

    site_url = _get_site_url(request)
    articles = list(getattr(NewsAggregator, "_news_cache", []) or [])
    if not articles:
        try:
            NewsAggregator.sync_list_cache_from_db(force=False)
        except Exception as e:
            logger.warning("sitemap.xml: 一覧キャッシュ同期に失敗: %s", e)
        articles = list(getattr(NewsAggregator, "_news_cache", []) or [])

    if articles and site_url:
        xml = render_sitemap(site_url, articles)
        if xml:
            logger.debug("sitemap.xml: %d 件の /topic/ を返します", len(articles))
            return Response(content=xml, media_type="application/xml; charset=utf-8")

    xml = read_sitemap_snapshot()
    if xml:
        logger.info("sitemap.xml: キャッシュ空のためスナップショットをフォールバック返却")
        return Response(content=xml, media_type="application/xml; charset=utf-8")

    today = datetime.now().date().isoformat()
    category_lines = [
        f"  <url><loc>{site_url}/topics/{slug}</loc><lastmod>{today}</lastmod><changefreq>hourly</changefreq><priority>0.8</priority></url>"
        for slug in CATEGORY_PAGES
    ]
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        f"  <url><loc>{site_url}/</loc><lastmod>{today}</lastmod><changefreq>hourly</changefreq><priority>1.0</priority></url>",
        f"  <url><loc>{site_url}/news</loc><lastmod>{today}</lastmod><changefreq>hourly</changefreq><priority>1.0</priority></url>",
        f"  <url><loc>{site_url}/trend</loc><lastmod>{today}</lastmod><changefreq>hourly</changefreq><priority>0.8</priority></url>",
        f"  <url><loc>{site_url}/search</loc><lastmod>{today}</lastmod><changefreq>daily</changefreq><priority>0.7</priority></url>",
        f"  <url><loc>{site_url}/ai</loc><lastmod>{today}</lastmod><changefreq>daily</changefreq><priority>0.6</priority></url>",
        f"  <url><loc>{site_url}/about</loc><lastmod>{today}</lastmod><changefreq>monthly</changefreq><priority>0.5</priority></url>",
        f"  <url><loc>{site_url}/personas</loc><lastmod>{today}</lastmod><changefreq>monthly</changefreq><priority>0.5</priority></url>",
        *category_lines,
        "</urlset>",
    ]
    return Response(content="\n".join(lines), media_type="application/xml; charset=utf-8")


@router.get("/sitemap-news.xml")
async def sitemap_news_xml(request: Request):
    """Google ニュース用 sitemap-news.xml。直近48時間の記事のみ収録。"""
    from datetime import timedelta, timezone

    site_url = _get_site_url(request)
    articles = list(getattr(NewsAggregator, "_news_cache", []) or [])
    if not articles:
        try:
            NewsAggregator.sync_list_cache_from_db(force=False)
        except Exception:
            pass
        articles = list(getattr(NewsAggregator, "_news_cache", []) or [])

    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)

    def _pub_dt(article):
        for attr in ("added_at", "published"):
            dt = getattr(article, attr, None)
            if not dt:
                continue
            if hasattr(dt, "tzinfo") and dt.tzinfo is None:
                from zoneinfo import ZoneInfo as _ZI
                dt = dt.replace(tzinfo=_ZI("Asia/Tokyo"))
            return dt
        return None

    def _pub_iso(article) -> str:
        dt = _pub_dt(article)
        if dt and hasattr(dt, "isoformat"):
            return dt.isoformat()
        return datetime.now(timezone.utc).isoformat()

    recent = [a for a in articles if (_pub_dt(a) or cutoff) >= cutoff]

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"',
        '        xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">',
    ]
    for article in recent[:1000]:
        title = (getattr(article, "title", "") or "").strip().replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        loc = f"{site_url}{article_url_path(article)}".replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        pub_date = _pub_iso(article)
        lines += [
            "  <url>",
            f"    <loc>{loc}</loc>",
            "    <news:news>",
            "      <news:publication>",
            "        <news:name>知リポAI</news:name>",
            "        <news:language>ja</news:language>",
            "      </news:publication>",
            f"      <news:publication_date>{pub_date}</news:publication_date>",
            f"      <news:title>{title}</news:title>",
            "    </news:news>",
            "  </url>",
        ]
    lines.append("</urlset>")
    return Response(content="\n".join(lines), media_type="application/xml; charset=utf-8")


@router.api_route("/", methods=["GET", "POST", "HEAD", "OPTIONS"])
async def root_home(request: Request, page: int = 1, keyword: str = ""):
    """トップは論文一覧。キーワード検索は従来どおりニュース一覧へ誘導"""
    if request.method == "POST":
        return {"message": "ok"}
    if request.method == "OPTIONS":
        return Response(status_code=200)
    if request.method in ("GET", "HEAD"):
        keyword = (keyword or "").strip()
        if keyword:
            q = [("keyword", keyword)]
            if page > 1:
                q.append(("page", str(page)))
            return RedirectResponse(url="/news?" + urlencode(q), status_code=302)
        try:
            return _render_papers_page(request, page)
        except Exception as e:
            logger.warning("papers page fallback: %s", e)
            _ph = _public_html_cache_headers()
            return templates.TemplateResponse(
                "papers.html",
                {
                    "request": request,
                    "papers_by_category": [],
                    "pagination": {"page": 1, "per_page": 24, "total": 0, "total_pages": 1, "has_prev": False, "has_next": False},
                    "has_papers": False,
                    "site_url": _get_site_url(request),
                    "page": 1,
                    "top_recommendations": [],
                    "papers_breadcrumb_jsonld": None,
                    "papers_itemlist_jsonld": None,
                },
                headers=_ph or None,
            )
    return Response(status_code=405)


@router.api_route("/news", methods=["GET", "POST", "HEAD", "OPTIONS"])
async def news_index(request: Request, page: int = 1, keyword: str = ""):
    """ニュース一覧（ジャンル別）。初回は1ページ分のみ表示し、以降は無限スクロール。"""
    if request.method == "POST":
        return {"message": "ok"}
    if request.method == "OPTIONS":
        return Response(status_code=200)
    from app.services.news_aggregator import ITEMS_PER_PAGE
    keyword = (keyword or "").strip()
    all_news = [a for a in NewsAggregator.get_news() if (a.category or "") != "研究・論文"]
    filtered = all_news
    if keyword:
        kw_lower = keyword.lower()
        filtered = [a for a in all_news if kw_lower in (a.title or "").lower() or kw_lower in (a.summary or "").lower()]
    per_page = ITEMS_PER_PAGE
    total = len(filtered)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    page_items = filtered[start : start + per_page]
    by_cat: dict[str, list] = {}
    for a in filtered:
        t = _news_tab_filter_category(a)
        by_cat.setdefault(t, []).append(a)
    news_category_order = _news_tab_category_order_for(by_cat)
    news_by_category = [(c, by_cat.get(c, [])) for c in news_category_order]
    news_tab_filter_cat = {a.id: _news_tab_filter_category(a) for a in filtered}
    pagination = {"page": page, "per_page": per_page, "total": total, "total_pages": total_pages, "has_prev": page > 1, "has_next": page < total_pages}
    try:
        trends = NewsAggregator.get_trends()
    except Exception:
        trends = []
    added_one = None
    for _, items in news_by_category:
        for item in items:
            _ensure_japanese(item)
            if not item.image_url:
                item.image_url = get_image_url(item.id, 400, 225)
            elif item.image_url and not item.image_url.startswith("http"):
                item.image_url = get_image_url(item.image_url, 400, 225)
    top_recommendations: list = []
    featured_items = _get_featured_items_with_persona(all_news, max_items=3)
    featured = featured_items[0] if featured_items else None
    latest_consultations = _get_latest_consultations(limit=3)
    site_url = _get_site_url(request)
    og_image = f"{site_url}/static/og/og-default.jpg"
    flat_news = [it for _, items in news_by_category for it in items]
    news_breadcrumb_jsonld = _build_breadcrumb_jsonld(
        [("ホーム", f"{site_url}/"), ("AIニュースアーカイブ", f"{site_url}/news")]
    )
    news_itemlist_jsonld = _build_itemlist_jsonld(
        page_name="AIニュースアーカイブ一覧",
        site_url=site_url,
        items=flat_news,
    )
    page_jsonld = _build_site_graph_jsonld(
        site_url=site_url,
        page_url=f"{site_url}/news",
        page_name="AIニュースまとめ — 最新情報を人工知能が解説 | 知リポAI",
        page_description=(
            "国内外の最新ニュースをAIが解説してまとめ。"
            "NHK・BBC・Reuters・TechCrunchなど信頼メディアの情報を、"
            "偉人AIが背景・ポイント・影響をわかりやすく解説。"
        ),
        extra_nodes=[news_breadcrumb_jsonld, news_itemlist_jsonld],
    )
    from app.services.markdown_for_agents import accepts_markdown, build_list_markdown, build_markdown_response

    if accepts_markdown(request):
        md_body, md_frontmatter, md_jsonld = build_list_markdown(
            title="AIニュースまとめ — 最新情報を人工知能が解説 | 知リポAI",
            description=(
                "国内外の最新ニュースをAIが解説してまとめ。"
                "NHK・BBC・Reuters・TechCrunchなど信頼メディアの情報を、"
                "偉人AIが背景・ポイント・影響をわかりやすく解説。"
            ),
            page_url=f"{site_url}/news",
            site_url=site_url,
            items=flat_news,
            page_jsonld=page_jsonld,
            list_heading="AIニュースアーカイブ",
        )
        return build_markdown_response(md_body, frontmatter=md_frontmatter, jsonld=md_jsonld)
    _ph = _public_html_cache_headers()
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "news_by_category": news_by_category,
            "page_items": page_items,
            "news_tab_filter_cat": news_tab_filter_cat,
            "trends": trends,
            "pagination": pagination,
            "added_one": added_one,
            "site_url": site_url,
            "og_image": og_image,
            "search_keyword": keyword,
            "top_recommendations": top_recommendations,
            "featured": featured,
            "featured_items": featured_items,
            "latest_consultations": latest_consultations,
            "news_breadcrumb_jsonld": news_breadcrumb_jsonld,
            "news_itemlist_jsonld": news_itemlist_jsonld,
            "page_jsonld": page_jsonld,
        },
        headers=_ph or None,
    )


@router.get("/api/news/page")
async def api_news_page(page: int = 1, keyword: str = ""):
    """無限スクロール用：ニュース一覧の指定ページHTMLを返す。"""
    from app.services.news_aggregator import ITEMS_PER_PAGE
    keyword = (keyword or "").strip()
    all_news = [a for a in NewsAggregator.get_news() if (a.category or "") != "研究・論文"]
    news = all_news
    if keyword:
        kw_lower = keyword.lower()
        news = [a for a in all_news if kw_lower in (a.title or "").lower() or kw_lower in (a.summary or "").lower()]
    total = len(news)
    per_page = ITEMS_PER_PAGE
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    items = news[start : start + per_page]
    for item in items:
        _ensure_japanese(item)
        if not item.image_url:
            item.image_url = get_image_url(item.id, 400, 225)
        elif not item.image_url.startswith("http"):
            item.image_url = get_image_url(item.image_url, 400, 225)
    import html as html_mod
    cards_html = ""
    for item in items:
        pub = item.published.strftime('%m/%d %H:%M') if item.published else ''
        title_safe = html_mod.escape(item.title or "")
        raw_summary = item.summary or ""
        summary_safe = html_mod.escape(raw_summary[:80])
        ellipsis = "..." if len(raw_summary) > 80 else ""
        source_safe = html_mod.escape(item.source or "")
        cat_safe = html_mod.escape(item.category or "")
        tab_cat_safe = html_mod.escape(_news_tab_filter_category(item))
        img_src = item.image_url or "/static/og/card-default.jpg"
        cards_html += f'''<article class="news-card animate-fade-in" data-category="{tab_cat_safe}">
<a href="{article_url_path(item)}" class="news-card-link">
<div class="news-card-body">
<div class="news-card-meta"><span class="news-card-source">{source_safe}</span><span class="news-card-time">{pub}</span></div>
<h3 class="news-title">{title_safe}</h3>
<p class="news-summary-line">{summary_safe}{ellipsis}</p>
</div>
<div class="news-card-image"><img src="{img_src}" alt="{title_safe}" loading="lazy" onerror="this.onerror=null;this.src='/static/og/card-default.jpg'"><span class="news-card-category">{cat_safe}</span></div>
</a></article>'''
    return {"html": cards_html, "page": page, "total_pages": total_pages}


@router.get("/api/papers/page")
async def api_papers_page(page: int = 1):
    """論文ページ用・無限スクロール：指定ページの論文カードHTMLを返す"""
    from app.services.news_aggregator import (
        NewsAggregator,
        SOURCE_TO_PAPER_DOMAIN,
        sort_papers_newest_first_inplace,
    )

    papers_by_category, pagination = NewsAggregator.get_papers_by_category(page=page)
    page_items: list = []
    for _, items in papers_by_category:
        page_items.extend(items)
    sort_papers_newest_first_inplace(page_items)
    _attach_paper_related_tags(page_items)
    import html as html_mod
    cards_html = ""
    for item in page_items:
        item.paper_domain = SOURCE_TO_PAPER_DOMAIN.get(item.source, "総合科学")
        _ensure_japanese(item)
        if not item.image_url:
            item.image_url = get_image_url(item.id, 400, 225)
        elif not item.image_url.startswith("http"):
            item.image_url = get_image_url(item.image_url, 400, 225)
        pub = item.published.strftime('%m/%d %H:%M') if item.published else ''
        title_safe = html_mod.escape(item.title or "")
        raw_summary = item.summary or ""
        summary_safe = html_mod.escape(raw_summary[:80])
        domain_safe = html_mod.escape(getattr(item, "paper_domain", None) or "総合科学")
        source_safe = html_mod.escape(item.source or "")
        ellipsis = "..." if len(raw_summary) > 80 else ""
        img_src = item.image_url or "/static/og/card-research.jpg"
        cards_html += f'''<article class="news-card animate-fade-in" data-category="{domain_safe}">
<a href="{article_url_path(item)}" class="news-card-link">
<div class="news-card-body">
<div class="news-card-meta"><span class="news-card-source">{source_safe}</span><span class="news-card-time">{pub}</span></div>
<h3 class="news-title">{title_safe}</h3>
<p class="news-summary-line">{summary_safe}{ellipsis}</p>
</div>
<div class="news-card-image"><img src="{img_src}" alt="{title_safe}" loading="lazy" onerror="this.onerror=null;this.src='/static/og/card-research.jpg'"><span class="news-card-category">{domain_safe}</span></div>
</a></article>'''
    return {"html": cards_html, "page": pagination["page"], "total_pages": pagination["total_pages"]}


@router.get("/papers")
async def papers_legacy_redirect(request: Request):
    """旧URL互換：トップ（論文一覧）へ統合"""
    q = request.url.query
    return RedirectResponse(url=("/?" + q) if q else "/", status_code=301)


@router.get("/topics/{slug}", response_class=HTMLResponse)
async def category_hub(request: Request, slug: str):
    """カテゴリハブページ（SEO向けインデックス可能なカテゴリ一覧）"""
    cat_info = CATEGORY_PAGES.get(slug)
    if not cat_info:
        raise HTTPException(status_code=404, detail="カテゴリが見つかりません")
    all_news = NewsAggregator.get_news()
    category_name = cat_info["category"]
    articles = [a for a in all_news if (getattr(a, "category", "") or "") == category_name]
    for item in articles:
        _ensure_japanese(item)
        if not item.image_url:
            item.image_url = get_image_url(item.id, 400, 225)
        elif not item.image_url.startswith("http"):
            item.image_url = get_image_url(item.image_url, 400, 225)
    site_url = _get_site_url(request)
    category_url = f"{site_url}/topics/{slug}"
    breadcrumb_jsonld = _build_breadcrumb_jsonld(
        [("ホーム", f"{site_url}/"), ("ニュース一覧", f"{site_url}/news"), (cat_info["label"], category_url)]
    )
    itemlist_jsonld = _build_itemlist_jsonld(
        page_name=cat_info["title"],
        site_url=site_url,
        items=articles[:30],
    )
    page_jsonld = _build_site_graph_jsonld(
        site_url=site_url,
        page_url=category_url,
        page_name=cat_info["title"],
        page_description=cat_info["desc"],
        extra_nodes=[breadcrumb_jsonld, itemlist_jsonld],
    )
    other_categories = {k: v for k, v in CATEGORY_PAGES.items() if k != slug}
    _ph = _public_html_cache_headers()
    return templates.TemplateResponse(
        "category.html",
        {
            "request": request,
            "cat_info": cat_info,
            "slug": slug,
            "articles": articles[:60],
            "site_url": site_url,
            "category_url": category_url,
            "page_jsonld": page_jsonld,
            "other_categories": other_categories,
        },
        headers=_ph or None,
    )


@router.get("/trend", response_class=HTMLResponse)
async def trend_page(request: Request):
    """トレンドページ：スコアが高い記事（従来どおり）"""
    all_news = NewsAggregator.get_news()
    trends = NewsAggregator.get_trends()
    trend_keywords = [t.keyword for t in trends]
    scored = []
    for item in all_news:
        text = f"{item.title} {item.summary}"
        score = sum(1 for kw in trend_keywords if kw.lower() in text.lower())
        scored.append((score, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    top_articles = [item for _, item in scored[:30]]
    for item in top_articles:
        _ensure_japanese(item)
        if not item.image_url:
            item.image_url = get_image_url(item.id, 400, 225)
        elif not item.image_url.startswith("http"):
            item.image_url = get_image_url(item.image_url, 400, 225)
    site_url = _get_site_url(request)
    page_jsonld = _build_site_graph_jsonld(
        site_url=site_url,
        page_url=f"{site_url}/trend",
        page_name="トレンド — いま注目度の高いニュース | 知リポAI",
        page_description="知リポAIのトレンドページ。AIが選ぶ注目度の高いニュースを一覧で確認できます。",
    )
    return templates.TemplateResponse(
        "trend.html",
        {
            "request": request,
            "articles": top_articles,
            "trends": trends,
            "site_url": site_url,
            "page_jsonld": page_jsonld,
        },
    )


@router.get("/ai", response_class=HTMLResponse)
async def ai_page(request: Request):
    """AIページ：キャラ推し投票 + 昨日のメモ・人格コメント"""
    ai_memo = None
    ai_personas = []
    try:
        from app.services.ai_daily import get_daily_ai_content
        daily = get_daily_ai_content()
        if daily:
            ai_memo = daily.get("memo", "")
            ai_personas = daily.get("persona_comments", [])
            if isinstance(ai_personas, list):
                patched = []
                for pc in ai_personas:
                    if isinstance(pc, dict):
                        p = dict(pc)
                        p["image_url"] = _persona_image_url(p.get("name"))
                        patched.append(p)
                ai_personas = patched
    except Exception:
        pass

    # キャラ一覧と投票数
    personas_with_votes = []
    try:
        from app.services.vote_service import get_persona_vote_counts
        vote_counts = get_persona_vote_counts()
        for p in PERSONAS:
            pv = _build_persona_view(p)
            pv["vote_count"] = vote_counts.get(int(p.get("id", -1)), 0)
            personas_with_votes.append(pv)
        personas_with_votes.sort(key=lambda x: x.get("vote_count", 0), reverse=True)
    except Exception:
        for p in PERSONAS:
            pv = _build_persona_view(p)
            pv["vote_count"] = 0
            personas_with_votes.append(pv)

    # アクティブな政策トピック（ナビリンク用）
    active_topic = None
    try:
        from app.services.vote_service import get_active_topics
        topics = get_active_topics()
        if topics:
            active_topic = topics[0]
    except Exception:
        pass

    # 過去の相談履歴
    past_consultations = []
    try:
        from app.services.consultation_store import get_consultations
        raw = get_consultations(limit=30)
        for c in raw:
            c["persona_image_url"] = _persona_image_url(c.get("persona_name"))
            past_consultations.append(c)
    except Exception:
        pass

    site_url = _get_site_url(request)
    page_jsonld = _build_site_graph_jsonld(
        site_url=site_url,
        page_url=f"{site_url}/ai",
        page_name="AI投票・政策立案 | 知リポAI",
        page_description="偉人AIキャラクターへの推し投票とAIによる政策提案。少子化・経済などのテーマについてAIが政策案を立案し、ユーザーが投票できます。",
    )
    return templates.TemplateResponse(
        "ai.html",
        {
            "request": request,
            "ai_memo": ai_memo,
            "ai_personas": ai_personas,
            "personas": personas_with_votes,
            "active_topic": active_topic,
            "past_consultations": past_consultations,
            "site_url": site_url,
            "page_jsonld": page_jsonld,
        },
    )


@router.post("/api/vote/persona/{persona_id}")
async def vote_persona(persona_id: int):
    """キャラ投票 API - 指定キャラの票数を +1 して返す。"""
    try:
        from app.services.vote_service import increment_persona_vote
        new_count = increment_persona_vote(persona_id)
        return {"ok": True, "persona_id": persona_id, "vote_count": new_count}
    except Exception as e:
        logger.warning("vote_persona error: %s", e)
        return {"ok": False, "persona_id": persona_id, "vote_count": 0}


@router.post("/api/vote/policy/{proposal_id}")
async def vote_policy(proposal_id: str):
    return {"ok": False, "proposal_id": proposal_id, "vote_count": 0}


@router.get("/policy", response_class=HTMLResponse)
async def policy_page(request: Request):
    return RedirectResponse(url="/ai", status_code=302)


@router.get("/policy/{topic_id}", response_class=HTMLResponse)
async def policy_topic_page(request: Request, topic_id: str):
    return RedirectResponse(url="/ai", status_code=302)


async def _policy_page_impl(request: Request, topic_id: str | None):
    try:
        from app.services.vote_service import get_active_topics, get_policy_proposals, get_policy_vote_counts
        topics = get_active_topics()
    except Exception:
        topics = []

    if not topics:
        return templates.TemplateResponse(
            "policy.html",
            {"request": request, "topic": None, "proposals": [], "other_topics": []},
        )

    if topic_id:
        topic = next((t for t in topics if t["id"] == topic_id), topics[0])
    else:
        topic = topics[0]

    other_topics = [t for t in topics if t["id"] != topic["id"]]

    try:
        proposals = get_policy_proposals(topic["id"])
        vote_counts = get_policy_vote_counts(topic["id"])
        for p in proposals:
            p["vote_count"] = vote_counts.get(p["id"], p.get("vote_count", 0))
    except Exception:
        proposals = []

    return templates.TemplateResponse(
        "policy.html",
        {
            "request": request,
            "topic": topic,
            "proposals": proposals,
            "other_topics": other_topics,
        },
    )


@router.post("/api/admin/generate-policy")
async def admin_generate_policy(request: Request, topic_key: str = "shoushika"):
    """管理者向け: 政策提案を手動生成する（バックグラウンド実行）。"""
    import threading
    def _run():
        try:
            from app.services.policy_ai_service import run_generate_and_save
            run_generate_and_save(topic_key)
        except Exception as e:
            logger.warning("admin_generate_policy 失敗: %s", e)
    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "message": f"トピック '{topic_key}' の生成をバックグラウンドで開始しました"}


@router.get("/about", response_class=HTMLResponse)
async def about_page(request: Request):
    """運営者情報ページ"""
    site_url = _get_site_url(request)
    faq_jsonld = {
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": "知リポAIとは何ですか？",
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": "知リポAIは、ブッダ・ニーチェ・アインシュタインなど歴史的偉人AIが最新のAI論文と国内外ニュースを日本語でわかりやすく解説する、無料のニュースメディアです。",
                },
            },
            {
                "@type": "Question",
                "name": "利用料金はかかりますか？",
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": "すべての機能が完全無料です。会員登録・ログインも不要で、記事閲覧・論文解説・AI投票・解説リクエストのすべてを無料でご利用いただけます。有料プランの設定は現時点で予定していません。",
                },
            },
            {
                "@type": "Question",
                "name": "AI解説の正確性はどのように担保されていますか？",
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": "arXiv・Nature・NHK・BBCなど編集方針の明確な信頼メディアのみをソースとして収集し、各記事ページに元記事・元論文へのリンクを掲載しています。AI解説はあくまで「読む入口」であり、内容の正確性を保証するものではありません。医療・法律・投資判断の根拠には使用しないでください。",
                },
            },
            {
                "@type": "Question",
                "name": "特定の偉人AIに解説をリクエストできますか？",
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": "LINEの公式アカウントからリクエストを受け付けています。記事URL・キーワードと担当してほしいキャラクター名を添えてメッセージを送ってください。キャラクター指定は任意で、内容確認のうえできる限り対応します。",
                },
            },
            {
                "@type": "Question",
                "name": "どのくらいの頻度で記事が更新されますか？",
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": "毎日8:30・13:00・16:30・19:00・22:00（日本時間）の5回、arXiv・Nature・Science・NHK・BBC・Reutersなど国内外の主要メディアから記事を収集し、AIが解説を生成・更新します。",
                },
            },
        ],
    }
    page_jsonld = _build_site_graph_jsonld(
        site_url=site_url,
        page_url=f"{site_url}/about",
        page_name="運営者情報・開発ストーリー - 知リポAI",
        page_description="なぜ知リポAIを作ったのか。ニュースキャスターをAIに置き換えるという発想、偉人を選んだ理由、そしてAI政策立案という壮大な目標について。",
        extra_nodes=[faq_jsonld],
    )
    return templates.TemplateResponse(
        "about.html",
        {
            "request": request,
            "site_url": site_url,
            "page_jsonld": page_jsonld,
        },
    )


@router.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request):
    """プライバシーポリシーページ"""
    return templates.TemplateResponse("privacy.html", {"request": request})


@router.get("/authors", response_class=HTMLResponse)
async def authors_page(request: Request):
    """著者情報ページ"""
    return templates.TemplateResponse("authors.html", {"request": request})


@router.get("/personas", response_class=HTMLResponse)
async def personas_page(request: Request):
    """14キャラクター紹介ページ"""
    personas = [_build_persona_view(p) for p in PERSONAS]
    site_url = _get_site_url(request)
    page_jsonld = _build_site_graph_jsonld(
        site_url=site_url,
        page_url=f"{site_url}/personas",
        page_name="偉人コメンテーター — ブッダ・ニーチェ・信長ほか | 知リポAI",
        page_description="ブッダ・織田信長・ニーチェ・ソクラテス・ダヴィンチなど時代と思想を超えたAIが、最新ニュースと研究論文を多角的に解説。各人物の哲学・価値観・名言をご紹介します。",
        extra_nodes=[_build_persona_person_node(p, site_url) for p in PERSONAS],
    )
    return templates.TemplateResponse(
        "personas.html",
        {
            "request": request,
            "personas": personas,
            "site_url": site_url,
            "page_jsonld": page_jsonld,
        },
    )


@router.get("/personas/{persona_id}", response_class=HTMLResponse)
async def persona_detail_page(request: Request, persona_id: int):
    """キャラクター詳細ページ"""
    target = next((p for p in PERSONAS if int(p.get("id", -1)) == int(persona_id)), None)
    if not target:
        raise HTTPException(status_code=404, detail="キャラクターが見つかりません")
    persona = _build_persona_view(target)
    other_personas = [_build_persona_view(p) for p in PERSONAS if int(p.get("id", -1)) != int(persona_id)][:6]
    site_url = _get_site_url(request)
    person_node = _build_persona_person_node(target, site_url)
    bio_short = str(target.get("bio", "") or "").replace("\n", " ").strip()[:120]
    page_jsonld = _build_site_graph_jsonld(
        site_url=site_url,
        page_url=f"{site_url}/personas/{persona_id}",
        page_name=f"{persona['name']} — 生涯・名言・価値観 | 知リポAI",
        page_description=f"{persona['name']}（{persona['type']}）の生涯と功績、名言、価値観。知リポAIで記事を解説するAIキャラクターです。{bio_short}",
        extra_nodes=[person_node],
    )
    try:
        from app.services.consultation_store import get_consultations
        persona_consultations = [c for c in get_consultations(limit=100) if c.get("persona_id") == persona_id]
    except Exception:
        persona_consultations = []
    return templates.TemplateResponse(
        "persona_detail.html",
        {
            "request": request,
            "persona": persona,
            "other_personas": other_personas,
            "site_url": site_url,
            "page_jsonld": page_jsonld,
            "persona_consultations": persona_consultations,
        },
    )


@router.get("/search", response_class=HTMLResponse)
async def search_page(request: Request, q: str = ""):
    """探すページ"""
    def _hira_to_kata(text: str) -> str:
        # ひらがな(3041-3096)をカタカナ(30A1-30F6)へ
        return "".join(chr(ord(ch) + 0x60) if "\u3041" <= ch <= "\u3096" else ch for ch in text)

    def _normalize_search_text(text: str) -> str:
        s = unicodedata.normalize("NFKC", (text or "")).strip().lower()
        s = _hira_to_kata(s)
        s = re.sub(r"\s+", " ", s)
        return s

    def _tokenize_query(query: str) -> list[str]:
        nq = _normalize_search_text(query)
        return [t for t in nq.split(" ") if t]

    def _extract_search_terms(text: str) -> list[str]:
        # 日本語/英数字の連続を候補語として抽出
        return re.findall(r"[a-z0-9ぁ-んァ-ヶー一-龯]+", text)

    def _fuzzy_hit(token: str, haystack_norm: str, terms: list[str]) -> bool:
        if not token:
            return False
        # まずは通常一致
        if token in haystack_norm:
            return True
        # 1文字はあいまい一致するとノイズが多すぎるため除外
        if len(token) <= 1:
            return False
        # 近い長さの語だけ比較して負荷と誤検知を抑える
        for term in terms:
            if not term:
                continue
            if abs(len(term) - len(token)) > max(2, len(token) // 2):
                continue
            ratio = SequenceMatcher(None, token, term).ratio()
            if ratio >= 0.78:
                return True
        return False

    q = (q or "").strip()
    results = []
    if q:
        all_news = NewsAggregator.get_news()
        tokens = _tokenize_query(q)
        scored: list[tuple[int, datetime, object]] = []

        for a in all_news:
            title_n = _normalize_search_text(a.title or "")
            summary_n = _normalize_search_text(a.summary or "")
            category_n = _normalize_search_text(a.category or "")
            source_n = _normalize_search_text(a.source or "")
            full_n = " ".join([title_n, summary_n, category_n, source_n]).strip()
            terms = _extract_search_terms(full_n)
            token_hits = [(_fuzzy_hit(t, full_n, terms)) for t in tokens]
            if not tokens or not all(token_hits):
                continue

            # タイトル一致を最優先し、次にカテゴリ/ソース、要約の順で重み付け
            score = 0
            for t in tokens:
                if t in title_n:
                    score += 12
                if t in category_n:
                    score += 7
                if t in source_n:
                    score += 6
                if t in summary_n:
                    score += 4
            # 完全一致に近い短いクエリはブースト
            joined = "".join(tokens)
            if joined and joined in title_n:
                score += 8

            scored.append((score, a.published or datetime.min, a))

        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        results = [x[2] for x in scored]
        for item in results:
            _ensure_japanese(item)
            if not item.image_url:
                item.image_url = get_image_url(item.id, 400, 225)
            elif not item.image_url.startswith("http"):
                item.image_url = get_image_url(item.image_url, 400, 225)
    site_url = _get_site_url(request)
    page_jsonld = _build_site_graph_jsonld(
        site_url=site_url,
        page_url=f"{site_url}/search",
        page_name="探す — 論文・ニュースを横断検索 | 知リポAI",
        page_description="キーワードを組み合わせて、AI論文もAIニュースも横断検索。知リポAIのすべてのコンテンツを一括で検索できます。",
    )
    return templates.TemplateResponse(
        "search.html",
        {
            "request": request,
            "query": q,
            "results": results,
            "site_url": site_url,
            "page_jsonld": page_jsonld,
        },
    )


@router.get("/saved", response_class=HTMLResponse)
async def saved_page(request: Request):
    """保存済み記事ページ（ローカルストレージベース）"""
    return templates.TemplateResponse("saved.html", {"request": request})


# 確認用ページで表示する直近の記事数
CONFIRM_PAGE_ARTICLE_LIMIT = 20


@router.get("/confirm", response_class=HTMLResponse)
async def confirm_page(request: Request):
    """確認用ページ：最新記事の1件取り込みと、記事の削除"""
    news = NewsAggregator.get_news()[:CONFIRM_PAGE_ARTICLE_LIMIT]
    for item in news:
        if not item.image_url:
            item.image_url = get_image_url(item.id, 400, 225)
        elif not item.image_url.startswith("http"):
            item.image_url = get_image_url(item.image_url, 400, 225)
    return templates.TemplateResponse(
        "confirm.html",
        {"request": request, "recent_articles": news}
    )


def _ensure_japanese(item):
    """保存時に日本語化済みのため、表示側では何もしない"""
    pass


def _get_site_url(request: Request) -> str:
    """サイトの絶対URL（末尾スラッシュなし）"""
    base = getattr(settings, "SITE_URL", "").strip().rstrip("/")
    if base:
        return base
    return str(request.base_url).rstrip("/")


def _meta_description_qa(title: str, summary: str | None, max_len: int = 160) -> str:
    """質問＋解答型のmeta description（SEO向け・「なぜ」「理由」「何」を入れる）"""
    t = (title or "").strip()
    s = (summary or "").replace("\n", " ").strip()[:200]
    if not t:
        return (s[: max_len - 3] + "...") if len(s) > max_len else s
    # SEO用に「なぜ」「理由」「何」を含む疑問形にする
    if "なぜ" in t or "理由" in t:
        question = f"{t}の理由とは？"
    elif "何" in t or "とは" in t:
        question = f"{t}を解説"
    else:
        question = f"{t}とは何？なぜ起きた？"
    if not s:
        return question[:max_len]
    answer = s[: max_len - len(question) - 4] + "..." if len(s) > max_len - len(question) - 2 else s
    return f"{question} {answer}"[:max_len]


def _iso_date(dt) -> str:
    try:
        if dt and hasattr(dt, "isoformat"):
            return dt.isoformat()
    except Exception:
        pass
    return datetime.now().isoformat()


def _webpage_id(page_url: str) -> str:
    return f"{page_url.rstrip('/')}/#webpage"


def _jsonld_strip_context(node: dict | None) -> dict | None:
    if not node:
        return None
    return {k: v for k, v in node.items() if k != "@context"}


def _build_site_graph_jsonld(
    *,
    site_url: str,
    page_url: str,
    page_name: str | None = None,
    page_description: str | None = None,
    extra_nodes: list[dict | None] | None = None,
) -> dict:
    """Organization + WebSite + WebPage を @graph で返す（ページ固有ノードは extra_nodes）"""
    base = site_url.rstrip("/")
    org_id = f"{base}/#organization"
    website_id = f"{base}/#website"
    logo_url = f"{base}/static/site-imgs/ロゴ.png"
    graph: list[dict] = [
        {
            "@id": org_id,
            "@type": "Organization",
            "name": SITE_JSONLD_TITLE,
            "url": base,
            "description": SITE_JSONLD_DESCRIPTION,
            "logo": {"@type": "ImageObject", "url": logo_url},
        },
        {
            "@id": website_id,
            "@type": "WebSite",
            "name": SITE_JSONLD_TITLE,
            "url": base,
            "description": SITE_JSONLD_DESCRIPTION,
            "inLanguage": "ja",
            "alternateName": ["チリポ", "ちりぽ", "知りぽAI"],
            "publisher": {"@id": org_id},
            "potentialAction": {
                "@type": "SearchAction",
                "target": f"{base}/search?q={{search_term_string}}",
                "query-input": "required name=search_term_string",
            },
        },
        {
            "@id": _webpage_id(page_url),
            "@type": "WebPage",
            "url": page_url,
            "name": page_name or SITE_JSONLD_TITLE,
            "description": page_description or SITE_JSONLD_DESCRIPTION,
            "inLanguage": "ja",
            "isPartOf": {"@id": website_id},
        },
    ]
    for node in extra_nodes or []:
        cleaned = _jsonld_strip_context(node)
        if cleaned:
            graph.append(cleaned)
    return {"@context": "https://schema.org", "@graph": graph}


def _default_site_graph_jsonld(request: Request) -> dict:
    site_url = _get_site_url(request)
    path = request.url.path or "/"
    page_url = f"{site_url}/" if path == "/" else f"{site_url.rstrip('/')}{path}"
    return _build_site_graph_jsonld(site_url=site_url, page_url=page_url)


templates.env.globals["default_site_graph_jsonld"] = _default_site_graph_jsonld
templates.env.globals["ga4_id"] = settings.GA4_MEASUREMENT_ID
templates.env.globals["clarity_id"] = settings.CLARITY_PROJECT_ID
templates.env.globals["article_url"] = article_url_path


def _build_article_jsonld(
    *,
    item,
    article_url: str,
    og_image: str,
    meta_desc: str,
    site_url: str,
    display_persona_ids: list[int] | None,
) -> dict:
    article_type = "ScholarlyArticle" if (item.category == "研究・論文") else "NewsArticle"
    # contributor: 記事に表示された偉人AIのみ（全14体をauthorに入れるのは不正確）
    contributors = []
    for pid in (display_persona_ids or []):
        try:
            p = PERSONAS[int(pid)]
            contributors.append(_build_persona_person_node(p, site_url))
        except Exception:
            continue
    base = site_url.rstrip("/")
    org_id = f"{base}/#organization"
    # author はメディア組織として知リポAI編集部を設定（Google News 要件に合わせる）
    editorial_author = {
        "@type": "Organization",
        "@id": org_id,
        "name": "知リポAI編集部",
        "url": f"{base}/authors",
        "logo": {"@type": "ImageObject", "url": f"{base}/static/site-imgs/ロゴ.png"},
    }
    jsonld = {
        "@context": "https://schema.org",
        "@type": article_type,
        "mainEntityOfPage": {"@type": "WebPage", "@id": _webpage_id(article_url)},
        "url": article_url,
        "headline": (item.title or "").strip(),
        "description": (meta_desc or "").strip(),
        "inLanguage": "ja",
        "datePublished": _iso_date(getattr(item, "published", None)),
        "dateModified": _iso_date(getattr(item, "added_at", None) or getattr(item, "published", None)),
        "author": editorial_author,
        "contributor": contributors,
        "publisher": {"@id": org_id},
        "image": [og_image] if og_image else [],
        "articleSection": item.category or "ニュース",
        "isAccessibleForFree": True,
    }
    if article_type == "NewsArticle":
        jsonld["speakable"] = {
            "@type": "SpeakableSpecification",
            "cssSelector": [".article-title", ".article-speakable-summary"],
        }
    source_link = (getattr(item, "link", "") or "").strip()
    if source_link:
        jsonld["isBasedOn"] = {
            "@type": "ScholarlyArticle" if article_type == "ScholarlyArticle" else "Article",
            "url": source_link,
            "publisher": {
                "@type": "Organization",
                "name": (getattr(item, "source", "") or "").strip(),
            },
        }
    return jsonld


def _build_breadcrumb_jsonld(items: list[tuple[str, str]]) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": i + 1,
                "name": name,
                "item": url,
            }
            for i, (name, url) in enumerate(items)
        ],
    }


def _build_itemlist_jsonld(*, page_name: str, site_url: str, items: list) -> dict:
    list_items = []
    for idx, it in enumerate(items[:30]):
        title = (getattr(it, "title", "") or "").strip()
        if not title:
            continue
        list_items.append(
            {
                "@type": "ListItem",
                "position": idx + 1,
                "url": f"{site_url.rstrip('/')}{article_url_path(it)}",
                "name": title,
            }
        )
    return {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": page_name,
        "itemListOrder": "https://schema.org/ItemListOrderDescending",
        "numberOfItems": len(list_items),
        "itemListElement": list_items,
    }


def _plain_text_for_copy(html_or_text: str | None, max_len: int = 1200) -> str:
    """コピー用に HTML タグを除いたプレーンテキスト（|striptags|tojson より型事故が少ない）"""
    import re as _re
    if not html_or_text:
        return ""
    t = _re.sub(r"<[^>]+>", " ", str(html_or_text))
    t = _re.sub(r"\s+", " ", t).strip()
    return t[:max_len] if len(t) > max_len else t


def _build_short_summary(quick_understand: dict | None, fallback_summary: str | None) -> str:
    """「1分で理解」用の要点まとめHTMLを生成。

    方針:
    - AIが生成した quick_understand（what/why/how）のうち what を優先表示
    - 取れない場合のみ fallback_summary を使う
    - シンプルな1文（〜120文字程度）で表示
    """
    import html as _html
    import re as _re

    def _one_line(text: str, max_len: int = 120) -> str:
        t = (text or "").replace("\n", " ").strip()
        t = _re.sub(r"\s+", " ", t).strip()
        if not t:
            return ""
        return (t[:max_len] + "…") if len(t) > max_len else t

    def _safe_text(v) -> str:
        return v.strip() if isinstance(v, str) else ""

    candidate = ""
    if isinstance(quick_understand, dict):
        candidate = _safe_text(quick_understand.get("what"))
        if not candidate:
            # what が無い場合は why/how を連結して最低限の要点にする
            parts = [
                p for p in [
                    _safe_text(quick_understand.get("why")),
                    _safe_text(quick_understand.get("how")),
                ] if p
            ]
            candidate = " / ".join(parts)

    if not candidate:
        candidate = (fallback_summary or "").strip()

    one = _one_line(candidate)
    if not one:
        return ""
    return f'<p class="article-text">{_html.escape(one)}</p>'


def _quick_points_non_empty(quick_understand: dict | None) -> bool:
    """1分で理解用の3要点（what/why/how）が1つ以上あるか"""
    def _safe_text(v) -> str:
        return v.strip() if isinstance(v, str) else ""

    if not isinstance(quick_understand, dict):
        return False
    for k in ("what", "why", "how"):
        if _safe_text(quick_understand.get(k)):
            return True
    return False


def _highlight_stats_in_text(text: str) -> str:
    """エスケープ後に数値・パーセント表記をスマートニュース風に下線強調。"""
    import html as _html
    import re as _re

    raw = (text or "").strip()
    if not raw:
        return ""
    esc = _html.escape(raw)

    def _hl(m):
        return f'<span class="sn-text-highlight">{m.group(0)}</span>'

    esc = _re.sub(r"\d+(?:[.,]\d+)?\s*[％％]|\d+(?:[.,]\d+)?\s*%", _hl, esc)
    return esc


def _build_article_lead_smartnews(quick_understand: dict | None, fallback_summary: str | None) -> str:
    """3要点が無いときのリード1段落（参照UIの下線強調に近づける）。"""
    import re as _re

    def _safe_text(v) -> str:
        return v.strip() if isinstance(v, str) else ""

    parts: list[str] = []
    if isinstance(quick_understand, dict):
        for k in ("what", "why"):
            t = _safe_text(quick_understand.get(k))
            if t:
                parts.append(t)
    raw = " ".join(parts) if parts else ""
    if not raw:
        raw = (fallback_summary or "").strip()
    raw = _re.sub(r"\s+", " ", raw).strip()
    if not raw:
        return ""
    raw = raw[:320] + ("…" if len(raw) > 320 else "")
    inner = _highlight_stats_in_text(raw)
    if not inner:
        return ""
    return f'<p class="article-lead-smartnews">{inner}</p>'


def _attach_paper_related_tags(items: list) -> None:
    """論文一覧カード向けに関連タグを付与（最大3件）"""
    if not items:
        return

    article_ids = [getattr(item, "id", "") for item in items if getattr(item, "id", "")]
    if not article_ids:
        for item in items:
            setattr(item, "related_tags", [])
        return

    # まずはメモリキャッシュから引く（/papers は同じ記事を何度も表示するため）
    with _PAPER_RELATED_TAGS_LOCK:
        cached_map = {aid: _PAPER_RELATED_TAGS_CACHE.get(aid, None) for aid in set(article_ids)}
    missing_ids = [aid for aid in article_ids if cached_map.get(aid) is None]
    missing_ids = list(dict.fromkeys(missing_ids))  # 順序維持＋重複排除

    def _apply_tags_to_items(tags_map: dict[str, list[str]]) -> None:
        for item in items:
            aid = getattr(item, "id", "")
            setattr(item, "related_tags", tags_map.get(aid, []))

    if not missing_ids:
        tags_map = {aid: cached_map[aid] for aid in cached_map if cached_map[aid] is not None}
        _apply_tags_to_items(tags_map)
        return

    # Neon の場合、関連タグだけバルクリードする
    tags_by_id: dict[str, list[str]] = {}
    _bulk_done = False
    try:
        from app.services.neon_store import use_neon, neon_get_related_tags_bulk
        if use_neon():
            tags_by_id = neon_get_related_tags_bulk(missing_ids, max_tags_per_article=3)
            _bulk_done = True
    except Exception:
        pass
    if not _bulk_done:
        # SQLite などは explanation_cache からまとめ読み
        try:
            from app.services.explanation_cache import get_cached_many

            cached_graph_map = get_cached_many(missing_ids)
            for aid, cached in (cached_graph_map or {}).items():
                graph = cached.get("paper_graph") if cached else {}
                raw_tags = graph.get("related_tags") if isinstance(graph, dict) else []
                if isinstance(raw_tags, list):
                    tags_by_id[aid] = [str(t).strip() for t in raw_tags if str(t).strip()][:3]
        except Exception:
            tags_by_id = {}

    # メモリキャッシュへ反映（見つからなかったIDは [] として保持して再読取を防ぐ）
    with _PAPER_RELATED_TAGS_LOCK:
        for aid in missing_ids:
            _PAPER_RELATED_TAGS_CACHE[aid] = tags_by_id.get(aid, [])

    # 併合して適用
    full_map: dict[str, list[str]] = {}
    with _PAPER_RELATED_TAGS_LOCK:
        for aid in set(article_ids):
            full_map[aid] = _PAPER_RELATED_TAGS_CACHE.get(aid, [])
    _apply_tags_to_items(full_map)


def _render_papers_page(request: Request, page: int = 1):
    """論文一覧（トップ `/` と共通）

    論文は研究・論文カテゴリの専用クエリ（Neon）または SQLite から取得する。
    初回は1ページ分のみ描画し、以降は無限スクロールで追加する。
    """
    from app.services.news_aggregator import (
        NewsAggregator as _NA,
        SOURCE_TO_PAPER_DOMAIN,
        paper_domains_ordered_for_page,
        sort_papers_newest_first_inplace,
    )

    papers_by_category, pagination = _NA.get_papers_by_category(page=page)
    all_papers: list = []
    for _, items in papers_by_category:
        all_papers.extend(items)
    sort_papers_newest_first_inplace(all_papers)
    paper_domains = paper_domains_ordered_for_page(all_papers)
    for item in all_papers:
        item.paper_domain = SOURCE_TO_PAPER_DOMAIN.get(item.source, "総合科学")

    # ── 画像 / 関連タグ補完 ───────────────────────────────────────────────────
    _attach_paper_related_tags(all_papers)
    for item in all_papers:
        _ensure_japanese(item)
        if not item.image_url:
            item.image_url = get_image_url(item.id, 400, 225)
        elif item.image_url and not item.image_url.startswith("http"):
            item.image_url = get_image_url(item.image_url, 400, 225)

    site_url = _get_site_url(request)
    flat_papers = all_papers
    papers_breadcrumb_jsonld = _build_breadcrumb_jsonld(
        [("ホーム", f"{site_url}/"), ("AI論文解説", f"{site_url}/")]
    )
    papers_itemlist_jsonld = _build_itemlist_jsonld(
        page_name="AI論文解説一覧",
        site_url=site_url,
        items=flat_papers,
    )
    base = site_url.rstrip("/")
    service_jsonld = {
        "@type": "Service",
        "@id": f"{base}/#service",
        "name": "知リポAI — AI論文・ニュース解説サービス",
        "url": base,
        "description": (
            "歴史的偉人AIキャラクター（ブッダ・ニーチェ・アインシュタインほか）が"
            "最新のAI論文・国内外ニュースを日本語でわかりやすく解説する無料Webサービス。"
            "arXiv・Nature・Science・NHK・BBC・Reutersなどから毎日収集・更新。"
        ),
        "serviceType": "AIニュース・論文解説",
        "inLanguage": "ja",
        "provider": {"@id": f"{base}/#organization"},
        "areaServed": "JP",
        "isAccessibleForFree": True,
    }
    page_jsonld = _build_site_graph_jsonld(
        site_url=site_url,
        page_url=f"{site_url}/",
        page_name="AI論文をわかりやすく解説 — arXiv・Nature・Science最新論文 | 知リポAI",
        page_description=(
            "AI論文・研究論文をわかりやすく日本語で解説。"
            "arXiv・Nature・Scienceから最新論文を毎日収集し、"
            "機械学習・LLM・深層学習論文の要約・解説を提供。"
        ),
        extra_nodes=[papers_breadcrumb_jsonld, papers_itemlist_jsonld, service_jsonld],
    )
    from app.services.markdown_for_agents import accepts_markdown, build_list_markdown, build_markdown_response

    if accepts_markdown(request):
        md_body, md_frontmatter, md_jsonld = build_list_markdown(
            title="AI論文をわかりやすく解説 — arXiv・Nature・Science最新論文 | 知リポAI",
            description=(
                "AI論文・研究論文をわかりやすく日本語で解説。"
                "arXiv・Nature・Scienceから最新論文を毎日収集し、"
                "機械学習・LLM・深層学習論文の要約・解説を提供。"
            ),
            page_url=f"{site_url}/",
            site_url=site_url,
            items=flat_papers,
            page_jsonld=page_jsonld,
            list_heading="AI論文解説一覧",
        )
        return build_markdown_response(md_body, frontmatter=md_frontmatter, jsonld=md_jsonld)
    has_papers = bool(all_papers)

    top_recommendations: list = []
    featured_items = _get_featured_items_with_persona(all_papers)
    featured = featured_items[0] if featured_items else None
    _consultations = _get_latest_consultations(limit=1)
    latest_consultation = _consultations[0] if _consultations else None

    _ph = _public_html_cache_headers()
    return templates.TemplateResponse(
        "papers.html",
        {
            "request": request,
            "all_papers": all_papers,
            "papers_by_category": papers_by_category,
            "paper_domains": paper_domains,
            "pagination": pagination,
            "has_papers": has_papers,
            "site_url": site_url,
            "page": pagination.get("page", 1),
            "top_recommendations": top_recommendations,
            "featured": featured,
            "latest_consultation": latest_consultation,
            "papers_breadcrumb_jsonld": papers_breadcrumb_jsonld,
            "papers_itemlist_jsonld": papers_itemlist_jsonld,
            "page_jsonld": page_jsonld,
        },
        headers=_ph or None,
    )


def _blocks_to_html(blocks: list) -> str:
    """ブロックをHTMLに変換。本文のみ表示。ミドルマン解説はフローティング吹き出し用の JSON データとして埋め込む"""
    safe = [b for b in (blocks or []) if isinstance(b, dict)]
    if not safe:
        return ""
    import html as _h
    import json as _json
    text_parts: list[str] = []
    float_items: list[dict] = []
    is_navigator = safe[0].get("type") == "navigator_section"
    nav_labels = {"facts": "ニュース", "background": "背景", "impact": "影響範囲", "prediction": "予測", "caution": "注意"}
    if is_navigator:
        for b in safe:
            if b.get("type") != "navigator_section" or not b.get("section"):
                continue
            body = (b.get("content") or "").strip()
            if not body:
                continue
            if b.get("section") == "facts":
                for p in body.split("\n\n"):
                    p = p.strip()
                    if p:
                        text_parts.append(_h.escape(p).replace("\n", "<br>"))
            else:
                label = nav_labels.get(b["section"], b["section"])
                float_items.append({"label": label, "body": _h.escape(body).replace("\n", "<br>")})
    else:
        for b in safe:
            if b.get("type") == "text":
                for p in (b.get("content") or "").strip().split("\n\n"):
                    p = p.strip()
                    if p:
                        text_parts.append(_h.escape(p).replace("\n", "<br>"))
            elif b.get("type") == "explain":
                c = (b.get("content") or "").strip()
                if c:
                    float_items.append({"label": "ミドルマン", "body": _h.escape(c).replace("\n", "<br>")})
    out = ['<div class="article-readflow">']
    for i, p in enumerate(text_parts):
        out.append(f'<p class="article-text" data-para="{i}">{p}</p>')
    if float_items:
        out.append(f'<script type="application/json" class="midorman-float-data">{_json.dumps(float_items, ensure_ascii=False)}</script>')
    out.append("</div>")
    return "".join(out)


def _editorial_take_from_blocks(blocks: list) -> str:
    """旧フォールバック。本文抜き出しは日本語が崩れやすいため表示しない。"""
    _ = blocks
    return ""


@router.get("/topic/{topic_id}", response_class=HTMLResponse)
async def topic_detail(request: Request, topic_id: str):
    """トピック詳細（URL: /topic/○○）・AI解説・SEO向け本文"""
    from app.services.explanation_cache import get_cached

    if not _topic_id_plausible(topic_id):
        raise HTTPException(status_code=404, detail="記事が見つかりません")
    # Bot が /topic/<16hex> を総当たりすると、毎回 DB を起こしてしまう。
    # 人間アクセスは「トップ/一覧から来る」ケースが大半なので、メモリに無いIDを bot には即404。
    from app.services.markdown_for_agents import accepts_markdown

    if _is_probably_bot_request(request) and not accepts_markdown(request):
        try:
            cached_list = getattr(NewsAggregator, "_news_cache", []) or []
            if not any(getattr(x, "id", None) == topic_id for x in cached_list):
                # スラッグ形式でもヒットするか確認
                if not _find_article_by_slug(topic_id):
                    raise HTTPException(status_code=404, detail="記事が見つかりません")
        except HTTPException:
            raise
        except Exception:
            pass
    # 旧 hex-ID URL: IDで直接引き当て → スラッグURLへ301リダイレクト
    item = NewsAggregator.get_article(topic_id)
    if item:
        slug_path = article_url_path(item)
        slug_key = slug_path.removeprefix("/topic/")
        if slug_key != topic_id:
            # 旧IDで来た → 正規のスラッグURLへ永久リダイレクト
            return RedirectResponse(url=slug_path, status_code=301)
    else:
        # スラッグURLで来た場合: スラッグ→記事を引き当て
        item = _find_article_by_slug(topic_id)
    if not item:
        raise HTTPException(status_code=404, detail="記事が見つかりません")
    _ensure_japanese(item)
    image_url = item.image_url or get_image_url(item.id, 800, 450)
    if image_url and not image_url.startswith("http"):
        image_url = get_image_url(image_url, 800, 450)
    site_url = _get_site_url(request)
    # canonical URL はスラッグURL
    slug_path = article_url_path(item)
    article_url = f"{site_url}{slug_path}"
    og_image = image_url if (image_url or "").startswith("http") else f"{site_url}{image_url}" if image_url else ""
    if not og_image or is_placeholder_image(og_image):
        from app.services.image_assets import category_og_path
        og_image = f"{site_url}{category_og_path(getattr(item, 'category', None))}"
    cached = get_cached(item.id)
    blocks = _sanitize_blocks(cached["blocks"]) if cached and cached.get("blocks") else []
    def _normalize_persona_ids(ids) -> list[int]:
        if not isinstance(ids, list):
            return []
        out: list[int] = []
        for v in ids:
            try:
                i = int(v)
            except Exception:
                continue
            if 0 <= i < len(PERSONAS):
                out.append(i)
        return out

    # 新形式: キャッシュに表示用3人分だけ保存されている場合はそのまま使用（API節約）
    cached_ids = _normalize_persona_ids(cached.get("display_persona_ids") if cached else None)
    cached_personas = cached.get("personas", []) if cached else []
    if cached_ids and isinstance(cached_personas, list) and len(cached_personas) == 3:
        display_persona_ids = cached_ids[:3]
        display_personas = [{**PERSONAS[i], "image_url": _persona_image_url(PERSONAS[i].get("name"))} for i in display_persona_ids]
        personas_data = [
            json.dumps(x, ensure_ascii=False) if isinstance(x, (dict, list))
            else (str(x) if x is not None else "")
            for x in cached_personas[:3]
        ]
    else:
        raw_personas = cached.get("personas", []) if cached else []
        all_personas_data = raw_personas if isinstance(raw_personas, list) else []
        import random as _rnd

        n_p = len(PERSONAS)
        display_indices = _rnd.sample(range(n_p), min(3, n_p)) if n_p else []
        display_personas = [{**PERSONAS[i], "image_url": _persona_image_url(PERSONAS[i].get("name"))} for i in display_indices]
        personas_data = [
            json.dumps(all_personas_data[i], ensure_ascii=False) if i < len(all_personas_data) and isinstance(all_personas_data[i], (dict, list))
            else (str(all_personas_data[i]) if i < len(all_personas_data) and all_personas_data[i] is not None else "")
            for i in display_indices
        ]
        display_persona_ids = display_indices
    # Firestore の Decimal 等で Jinja |tojson が落ちるのを防ぐ＋型崩れを正規化
    # dict の場合は json.dumps で変換（str() は Python式シングルクォートになりJS JSON.parseが失敗する）
    ps_wrapped = _json_safe_for_template(personas_data)
    if isinstance(ps_wrapped, list):
        personas_data = [
            json.dumps(x, ensure_ascii=False) if isinstance(x, (dict, list))
            else (str(x) if x is not None else "")
            for x in ps_wrapped
        ]
    quick_understand = _sanitize_quick_understand_for_page(cached.get("quick_understand") if cached else None)
    # 投票クイズ・論文ナレッジグラフ/クイズは当面非表示（キャッシュにあっても出さない）
    vote_data = None
    paper_graph = None
    paper_quiz = None
    deep_insights = None
    body_html = _blocks_to_html(blocks) if blocks else ""
    editorial_take = str((cached or {}).get("editorial_take") or "").strip() or _editorial_take_from_blocks(blocks)
    short_summary = _build_short_summary(quick_understand, item.summary)
    show_quick_points = _quick_points_non_empty(quick_understand)
    article_lead_html = (
        ""
        if show_quick_points
        else _build_article_lead_smartnews(quick_understand, item.summary)
    )
    quick_rows: dict[str, str] = {}
    if show_quick_points and isinstance(quick_understand, dict):
        for k in ("what", "why", "how"):
            v = quick_understand.get(k)
            t = v.strip() if isinstance(v, str) else ""
            if t:
                quick_rows[k] = _highlight_stats_in_text(t)
    meta_desc = _meta_description_qa(item.title, item.summary)
    # 見た目演出用（記事ごとに安定した値）
    readers_now = 20 + (abs(hash(topic_id)) % 130)
    copy_blurb = _plain_text_for_copy(short_summary) or (item.summary or "").strip()

    try:
        all_news = NewsAggregator.get_news()
    except Exception as e:
        logger.warning("topic_detail: get_news に失敗したため関連欄を省略します: %s", e)
        all_news = []
    next_article = prev_article = None
    for i, a in enumerate(all_news):
        if a.id == item.id:
            if i + 1 < len(all_news):
                next_article = all_news[i + 1]
            if i > 0:
                prev_article = all_news[i - 1]
            break

    from app.services.seo_internal_links import (
        list_hub_for_article,
        pick_latest_articles,
        pick_related_articles,
        pick_same_category_articles,
    )

    pool = [a for a in all_news if a.id != item.id]
    related = pick_related_articles(item, pool, limit=6)
    exclude_ids = {item.id, *(getattr(a, "id", "") for a in related)}
    same_category_articles = pick_same_category_articles(item, pool, exclude_ids, limit=4)
    exclude_ids |= {getattr(a, "id", "") for a in same_category_articles}
    latest_articles = pick_latest_articles(item, pool, exclude_ids, limit=5)
    list_hub_label, list_hub_path = list_hub_for_article(item)
    ai_recommended: list = []
    related_itemlist_jsonld = None
    if related or latest_articles:
        related_itemlist_jsonld = _build_itemlist_jsonld(
            page_name=f"{item.title} — 関連・最新",
            site_url=site_url,
            items=(related + latest_articles)[:12],
        )

    published_text = ""
    try:
        if getattr(item, "published", None):
            p = item.published
            if hasattr(p, "strftime"):
                published_text = p.strftime("%Y/%m/%d %H:%M")
            else:
                published_text = str(p)[:16]
    except Exception:
        published_text = ""
    article_jsonld = _build_article_jsonld(
        item=item,
        article_url=article_url,
        og_image=og_image,
        meta_desc=meta_desc,
        site_url=site_url,
        display_persona_ids=display_persona_ids,
    )
    page_jsonld = _build_site_graph_jsonld(
        site_url=site_url,
        page_url=article_url,
        page_name=(item.title or "").strip(),
        page_description=meta_desc,
        extra_nodes=[article_jsonld, related_itemlist_jsonld],
    )
    from app.services.markdown_for_agents import build_markdown_response, build_topic_markdown

    if accepts_markdown(request):
        md_body, md_frontmatter, md_jsonld = build_topic_markdown(
            item=item,
            article_url=article_url,
            meta_desc=meta_desc,
            og_image=og_image,
            blocks=blocks,
            quick_understand=quick_understand,
            personas_data=personas_data,
            display_personas=display_personas,
            page_jsonld=page_jsonld,
            related_articles=(related + latest_articles)[:8],
            site_url=site_url,
        )
        return build_markdown_response(md_body, frontmatter=md_frontmatter, jsonld=md_jsonld)
    _article_cat = (getattr(item, "category", None) or "").strip()
    mobile_nav_papers_highlight = _article_cat == "研究・論文"
    mobile_nav_news_highlight = not mobile_nav_papers_highlight
    if mobile_nav_papers_highlight:
        editorial_take_kicker = "ミドルマンが精査"
        editorial_take_title = "この論文の読みどころ"
    else:
        editorial_take_kicker = "ミドルマンが整理"
        editorial_take_title = "このニュースの先読み"
    all_personas_enriched = [{**p, "image_url": _persona_image_url(p.get("name"))} for p in PERSONAS]

    _ph = _public_html_cache_headers()
    return templates.TemplateResponse(
        "article.html",
        {
            "request": request,
            "article": item,
            "image_url": image_url,
            "personas": display_personas,
            "display_persona_ids": display_persona_ids,
            "all_personas": all_personas_enriched,
            "site_url": site_url,
            "canonical_url": article_url,
            "og_image": og_image,
            "blocks": blocks,
            "personas_data": personas_data,
            "body_html": body_html,
            "editorial_take": editorial_take,
            "editorial_take_kicker": editorial_take_kicker,
            "editorial_take_title": editorial_take_title,
            "short_summary": short_summary,
            "article_lead_html": article_lead_html,
            "show_quick_points": show_quick_points,
            "quick_rows": quick_rows,
            "meta_description": meta_desc,
            "next_article": next_article,
            "prev_article": prev_article,
            "related_articles": related,
            "same_category_articles": same_category_articles,
            "latest_articles": latest_articles,
            "list_hub_label": list_hub_label,
            "list_hub_path": list_hub_path,
            "ai_recommended": ai_recommended,
            "related_itemlist_jsonld": related_itemlist_jsonld,
            "quick_understand": quick_understand,
            "vote_data": vote_data,
            "paper_graph": paper_graph,
            "paper_quiz": paper_quiz,
            "deep_insights": deep_insights,
            "readers_now": readers_now,
            "published_text": published_text,
            "copy_blurb": copy_blurb,
            "article_jsonld": article_jsonld,
            "page_jsonld": page_jsonld,
            "mobile_nav_papers_highlight": mobile_nav_papers_highlight,
            "mobile_nav_news_highlight": mobile_nav_news_highlight,
            "midorman_image_url": _persona_image_url("ミドルマン"),
        },
        headers=_ph or None,
    )


@router.get("/article/{article_id}", response_class=HTMLResponse)
async def article_detail(request: Request, article_id: str):
    """旧URL: /topic/ へリダイレクト"""
    if not _topic_id_plausible(article_id):
        raise HTTPException(status_code=404, detail="記事が見つかりません")
    item = NewsAggregator.get_article(article_id)
    if not item:
        raise HTTPException(status_code=404, detail="記事が見つかりません")
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=article_url_path(item), status_code=301)


def _sanitize_blocks(blocks: list) -> list:
    """ブロックのcontentからHTML断片を除去（キャッシュ済み悪データ対策）"""
    out = []
    for b in (blocks or []):
        if not isinstance(b, dict):
            continue
        if "content" in b and b.get("content"):
            b = {**b, "content": sanitize_display_text(str(b["content"]))}
        out.append(b)
    return out


@router.get("/api/article/{article_id}/explain")
async def api_explain_article(article_id: str):
    """記事のAI解説を取得（従来形式・サイドパネル用）"""
    if not _topic_id_plausible(article_id):
        raise HTTPException(status_code=404, detail="記事が見つかりません")
    item = NewsAggregator.get_article(article_id)
    if not item:
        raise HTTPException(status_code=404, detail="記事が見つかりません")
    content = f"{item.title}\n\n{item.summary}"
    explanation = explain_article_with_ai(item.title, content)
    return {"explanation": explanation}


@router.get("/api/article/{article_id}/explain-inline")
async def api_explain_inline(article_id: str):
    """記事本文と解説が交互に入った構造で取得（従来API・互換用）"""
    if not _topic_id_plausible(article_id):
        raise HTTPException(status_code=404, detail="記事が見つかりません")
    item = NewsAggregator.get_article(article_id)
    if not item:
        raise HTTPException(status_code=404, detail="記事が見つかりません")
    data = generate_all_explanations(article_id, item.title, f"{item.title}\n\n{item.summary}", category=item.category)
    return {"blocks": _sanitize_blocks(data["blocks"])}


@router.get("/api/article/{article_id}/explanations")
async def api_all_explanations(article_id: str):
    """ミドルマン解説＋人格の意見を一括取得（キャッシュ優先・表示用3人分のみ生成）"""
    if not _topic_id_plausible(article_id):
        raise HTTPException(status_code=404, detail="記事が見つかりません")
    item = NewsAggregator.get_article(article_id)
    if not item:
        raise HTTPException(status_code=404, detail="記事が見つかりません")
    content = f"{item.title}\n\n{item.summary}"
    data = generate_all_explanations(article_id, item.title, content, category=item.category)
    # 新形式は3人分のみ→フロント互換のため14スロットで返す（該当3件のみ埋める）
    if data.get("display_persona_ids") is not None and len(data.get("personas", [])) == 3:
        full_personas = [""] * len(PERSONAS)
        for i, pid in enumerate(data["display_persona_ids"]):
            if 0 <= pid < len(full_personas):
                full_personas[pid] = data["personas"][i]
        return {"blocks": _sanitize_blocks(data["blocks"]), "personas": full_personas}
    return {"blocks": _sanitize_blocks(data["blocks"]), "personas": data["personas"]}


@router.get("/api/article/{article_id}/opinion/{persona_id}")
async def api_persona_opinion(article_id: str, persona_id: int):
    """表示用3人のうち1人の意見を取得（キャッシュ経由。当該記事で選ばれていない人格は空）"""
    if persona_id < 0 or persona_id >= len(PERSONAS):
        raise HTTPException(status_code=404, detail="人格が見つかりません")
    if not _topic_id_plausible(article_id):
        raise HTTPException(status_code=404, detail="記事が見つかりません")
    item = NewsAggregator.get_article(article_id)
    if not item:
        raise HTTPException(status_code=404, detail="記事が見つかりません")
    data = generate_all_explanations(article_id, item.title, f"{item.title}\n\n{item.summary}", category=item.category)
    if data.get("display_persona_ids") is not None and persona_id in data["display_persona_ids"]:
        idx = data["display_persona_ids"].index(persona_id)
        opinion = data["personas"][idx] if idx < len(data["personas"]) else ""
    else:
        opinion = data["personas"][persona_id] if persona_id < len(data["personas"]) else ""
    return {"persona": PERSONAS[persona_id], "opinion": opinion}


@router.get("/api/status")
async def api_status():
    """状態確認（高速）。DB読取をせずメモリ情報のみ返す"""
    displayable = len(getattr(NewsAggregator, "_news_cache", []) or [])
    processed_count = int(getattr(NewsAggregator, "_last_processed_count", 0) or 0)
    try:
        from app.config import settings
        from app.utils.llm_client import ai_provider, is_ai_configured

        has_key = is_ai_configured()
    except Exception:
        has_key = False

    return {
        "articles_in_db": displayable,
        "ai_processed": processed_count,
        "displayable": displayable,
        "openai_key_set": has_key,
        "ai_provider": ai_provider(),
        "ai_configured": has_key,
    }


@router.get("/api/news/refresh")
async def api_refresh_news():
    """ニュース・トレンドを手動更新（バックグラウンドで実行、即応答）"""
    import threading

    def _refresh():
        NewsAggregator.get_trends(force_refresh=True)  # トレンドは速い
        if not is_rss_and_ai_disabled():
            NewsAggregator.get_news(force_refresh=True)  # 記事はAI処理で遅い（無効時はスキップ）

    threading.Thread(target=_refresh, daemon=True).start()
    msg = "更新を開始しました" if not is_rss_and_ai_disabled() else "トレンドのみ更新しました（RSS・AIはこの環境では無効です）"
    return {"status": "ok", "message": msg}


def _is_admin(request: Request, x_admin_secret: str | None = Header(None, alias="X-Admin-Secret")) -> bool:
    """セッションまたは X-Admin-Secret ヘッダで管理者か判定"""
    admin = (getattr(settings, "ADMIN_SECRET", "") or "").strip()
    if not admin:
        return False
    if x_admin_secret and x_admin_secret.strip() == admin:
        return True
    try:
        return request.session.get("admin") is True
    except Exception as e:
        logger.warning("管理判定: セッション読み取り失敗（Cookie 破損や鍵不一致の可能性）: %s", e)
        return False


def _is_cache_refresh_notify_authorized(request: Request, x_admin_secret: str | None) -> bool:
    """キャッシュ更新 API: 管理者に加え、CACHE_REFRESH_SECRET が一致すれば許可（本番とローカルで ADMIN_SECRET を揃えなくてよい）。"""
    if _is_admin(request, x_admin_secret):
        return True
    cr = (getattr(settings, "CACHE_REFRESH_SECRET", "") or "").strip()
    if cr and x_admin_secret and x_admin_secret.strip() == cr:
        return True
    return False


@router.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    """管理者ログインフォーム表示"""
    if not getattr(settings, "ADMIN_SECRET", ""):
        return templates.TemplateResponse("admin_login.html", {"request": request, "error": "管理機能は無効です（ADMIN_SECRET 未設定）"})
    if request.session.get("admin"):
        return RedirectResponse(url="/admin", status_code=302)
    err = "シークレットが正しくありません" if request.query_params.get("error") == "invalid" else None
    return templates.TemplateResponse("admin_login.html", {"request": request, "error": err})


@router.post("/admin/login", response_class=RedirectResponse)
async def admin_login_submit(request: Request, secret: str = Form(...)):
    """管理者ログイン処理"""
    if not getattr(settings, "ADMIN_SECRET", ""):
        raise HTTPException(status_code=403, detail="管理機能は無効です")
    if secret.strip() != settings.ADMIN_SECRET:
        return RedirectResponse(url="/admin/login?error=invalid", status_code=302)
    request.session["admin"] = True
    return RedirectResponse(url="/admin", status_code=302)


@router.get("/admin/logout", response_class=RedirectResponse)
async def admin_logout(request: Request):
    """管理者ログアウト"""
    request.session.pop("admin", None)
    return RedirectResponse(url="/", status_code=302)


@router.get("/admin", response_class=HTMLResponse)
async def admin_manual_article_page(request: Request):
    """手動記事追加フォーム（管理者のみ）"""
    if not getattr(settings, "ADMIN_SECRET", ""):
        raise HTTPException(status_code=403, detail="管理機能は無効です")
    if not request.session.get("admin"):
        return RedirectResponse(url="/admin/login", status_code=302)
    return templates.TemplateResponse("admin_manual_article.html", {"request": request})


def _do_create_manual_article_sync(title: str, summary: str, link: str = "", source: str = "編集部") -> dict:
    """手動記事をAIで生成して保存（同期・スレッド実行用）。記事保存成功後に save_cache。"""
    article_id = "manual-" + uuid.uuid4().hex[:16]
    content = sanitize_display_text(f"{title}\n\n{summary}")[:20000]
    data = generate_all_explanations(article_id, title, content, category="総合", persist_cache=False)
    blocks = data.get("blocks", [])
    if not blocks:
        return {"status": "error", "article_id": None, "message": "AIによる記事生成に失敗しました"}
    item = NewsItem(
        id=article_id,
        title=title,
        link=link or "#",
        summary=summary[:4000],
        published=datetime.now(),
        source=source or "編集部",
        category="総合",
        image_url=None,
    )
    if not save_article(item):
        return {"status": "error", "article_id": None, "message": "記事の保存に失敗しました"}
    personas = list(data.get("personas") or ["", "", ""])
    while len(personas) < 3:
        personas.append("")
    personas = personas[:3]
    dips = list(data.get("display_persona_ids") or [])
    personas = upgrade_personas_with_claude_if_configured(
        title, str(data.get("navigator_summary") or ""), dips, personas
    )
    save_cache(
        article_id,
        blocks,
        personas,
        display_persona_ids=data.get("display_persona_ids"),
        quick_understand=data.get("quick_understand"),
        vote_data=data.get("vote_data"),
        paper_graph=data.get("paper_graph"),
        paper_quiz=data.get("paper_quiz"),
        deep_insights=data.get("deep_insights"),
        editorial_take=data.get("editorial_take"),
    )
    NewsAggregator.get_news(force_refresh=not is_rss_and_ai_disabled())
    try:
        from app.services.render_notifier import notify_render_cache_refresh

        notify_render_cache_refresh(reason="manual_added:1")
    except Exception:
        pass
    return {"status": "ok", "article_id": article_id}


@router.post("/api/admin/article/manual")
async def api_admin_article_manual(
    request: Request,
    x_admin_secret: str | None = Header(None, alias="X-Admin-Secret"),
):
    """手動で概要を送り、AIが理解ナビゲーター形式の記事を生成して追加（管理者のみ）"""
    if not _is_admin(request, x_admin_secret):
        raise HTTPException(status_code=403, detail="管理者のみ利用できます")
    if is_rss_and_ai_disabled():
        raise HTTPException(status_code=503, detail="この環境ではRSS取得・AI要約は無効です。手動記事追加はローカル等で実行してください。")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON で title, summary を送ってください")
    title = (body.get("title") or "").strip()
    summary = (body.get("summary") or "").strip()
    if not title or not summary:
        raise HTTPException(status_code=400, detail="タイトルと概要は必須です")
    link = (body.get("link") or "").strip()
    source = (body.get("source") or "編集部").strip() or "編集部"
    import asyncio
    loop = asyncio.get_event_loop()
    result = await asyncio.wait_for(
        loop.run_in_executor(
            None,
            lambda: _do_create_manual_article_sync(title, summary, link, source),
        ),
        timeout=180.0,
    )
    return result


@router.post("/api/admin/article/{article_id}/clear-cache")
async def api_clear_article_cache(article_id: str):
    """記事の解説キャッシュを削除（再生成させる）"""
    from app.services.explanation_cache import delete_cache
    deleted = delete_cache(article_id)
    return {"status": "ok", "deleted": deleted, "message": "キャッシュを削除しました。次回アクセスで再生成されます。"}


@router.post("/api/admin/article/{article_id}/delete")
async def api_delete_article(article_id: str):
    """記事を完全に削除（解説キャッシュ＋記事DBから削除）"""
    from app.services.explanation_cache import delete_cache
    from app.services.article_cache import delete_article as delete_article_from_db
    deleted_cache = delete_cache(article_id)
    deleted_article = delete_article_from_db(article_id)
    NewsAggregator.get_news(force_refresh=not is_rss_and_ai_disabled())
    return {
        "status": "ok",
        "deleted": deleted_cache or deleted_article,
        "message": "記事を削除しました。",
    }


@router.get("/api/admin/seed-articles")
async def api_seed_articles():
    """RSSからミドルマンAI解説付きで記事を投入（新着5件）"""
    if is_rss_and_ai_disabled():
        raise HTTPException(status_code=503, detail="この環境ではRSS取得・AI要約は無効です。ローカル等で実行してください。")
    from app.services.rss_service import fetch_rss_news
    from app.services.article_processor import process_new_rss_articles
    from app.services.explanation_cache import get_cached_article_ids

    news = fetch_rss_news()
    added = process_new_rss_articles(news, max_per_run=5)
    NewsAggregator.get_news(force_refresh=True)
    total = len(get_cached_article_ids())
    return {"status": "ok", "added": added, "total": total}


@router.post("/api/admin/seed-curated")
async def api_seed_curated(
    request: Request,
    x_admin_secret: str | None = Header(None, alias="X-Admin-Secret"),
    max: int = 30,
):
    """
    curated_articles.json の記事を既存パイプラインで記事化する（管理者のみ）。
    リクエストボディに JSON 配列を渡すと curated_articles.json を上書きしてから処理する。
    ボディなし（空）の場合はサーバー上の既存 curated_articles.json をそのまま処理する。
    """
    if not _is_admin(request, x_admin_secret):
        raise HTTPException(status_code=403, detail="管理者のみ利用できます")
    if is_rss_and_ai_disabled():
        raise HTTPException(status_code=503, detail="この環境ではAI要約は無効です。")

    # ボディに JSON 配列が渡された場合、サーバー上の curated_articles.json を上書きする
    from pathlib import Path as _Path
    _curated_file = _Path(__file__).resolve().parent.parent.parent / "curated_articles.json"
    try:
        body_bytes = await request.body()
        if body_bytes and body_bytes.strip():
            articles_data = json.loads(body_bytes)
            if isinstance(articles_data, list) and articles_data:
                _curated_file.write_text(
                    json.dumps(articles_data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                logger.info("seed-curated: %d 件の記事リストをアップロードしました", len(articles_data))
    except Exception as e:
        logger.warning("seed-curated: ボディ解析エラー（既存ファイルをそのまま使用）: %s", e)

    import asyncio
    from app.services.article_seed_from_curated import process_curated_articles
    from app.services.explanation_cache import get_cached_article_ids
    added = await asyncio.get_event_loop().run_in_executor(
        None, lambda: process_curated_articles(max_per_run=max)
    )
    NewsAggregator.get_news(force_refresh=True)
    total = len(get_cached_article_ids())
    return {"status": "ok", "added": added, "total": total}


@router.get("/api/admin/cache/refresh")
async def api_admin_cache_refresh_get():
    """ブラウザで開いたとき用。実処理は POST のみ。"""
    return JSONResponse(
        status_code=200,
        content={
            "ok": False,
            "reason": "Method Not Allowed for cache refresh",
            "hint": "アドレスバーでの表示は GET のためキャッシュは更新されません。HTTP POST とヘッダ X-Admin-Secret が必要です。",
            "use_method": "POST",
            "header": "X-Admin-Secret: （ADMIN_SECRET または CACHE_REFRESH_SECRET と同一）",
            "curl_example": 'curl -X POST -H "X-Admin-Secret: YOUR_SECRET" https://tiripo-ai.site/api/admin/cache/refresh',
        },
    )


@router.post("/api/admin/cache/refresh")
async def api_admin_cache_refresh(
    request: Request,
    x_admin_secret: str | None = Header(None, alias="X-Admin-Secret"),
):
    """（案A）外部（ローカル記事化）からの通知で、Render 側のメモリキャッシュを更新する。"""
    if not _is_cache_refresh_notify_authorized(request, x_admin_secret):
        raise HTTPException(status_code=403, detail="管理者のみ利用できます")
    import asyncio

    try:
        from app.services.explanation_cache import invalidate_ids_cache

        await asyncio.to_thread(invalidate_ids_cache)
    except Exception as e:
        logger.warning("cache/refresh: invalidate_ids_cache: %s", e)
    try:
        await asyncio.to_thread(lambda: NewsAggregator.sync_list_cache_from_db(force=True))
    except Exception as e:
        logger.warning("cache/refresh: sync_list_cache_from_db: %s", e)
    try:
        await asyncio.to_thread(lambda: NewsAggregator._invalidate_papers_cache())
    except Exception as e:
        logger.warning("cache/refresh: _invalidate_papers_cache: %s", e)
    cached = len(getattr(NewsAggregator, "_news_cache", []) or [])
    sitemap_topics = 0
    try:
        NewsAggregator._refresh_sitemap_snapshot()
        sitemap_topics = cached
    except Exception as e:
        logger.warning("cache/refresh: sitemap 更新: %s", e)
    return {"status": "ok", "cached": cached, "sitemap_topics": sitemap_topics}


@router.get("/api/admin/claude-usage")
async def api_admin_claude_usage(
    request: Request,
    x_admin_secret: str | None = Header(None, alias="X-Admin-Secret"),
):
    """Claude 使用量の概算（項目別）を返す。"""
    if not _is_admin(request, x_admin_secret):
        raise HTTPException(status_code=403, detail="管理者のみ利用できます")
    try:
        from app.services.claude_researcher import get_claude_usage_stats

        return {"status": "ok", "usage": get_claude_usage_stats()}
    except Exception as e:
        return {"status": "error", "usage": {}, "message": str(e)}


@router.post("/api/admin/sync-meta")
async def api_admin_sync_meta(
    request: Request,
    x_admin_secret: str | None = Header(None, alias="X-Admin-Secret"),
):
    """
    一覧キャッシュと解説 ID キャッシュを DB 状態に合わせて強制リフレッシュする（旧 Firestore メタ同期の代替）。
    """
    if not _is_admin(request, x_admin_secret):
        raise HTTPException(status_code=403, detail="管理者のみ利用できます")
    from app.services.explanation_cache import invalidate_ids_cache

    invalidate_ids_cache()
    NewsAggregator.sync_list_cache_from_db(force=True)
    return {"status": "ok", "synced": 0, "message": "一覧キャッシュを DB から再同期しました"}


def _do_seed_one_sync():
    """RSS取得→1件だけ記事化（重い処理を同期的に実行・スレッドから呼ぶ用）"""
    from app.services.rss_service import fetch_rss_news
    from app.services.article_processor import process_new_rss_articles
    news = fetch_rss_news()
    if not news:
        return {"status": "error", "article_id": None, "message": "RSSから記事を取得できませんでした。フィードURLやネットワークを確認してください。"}
    added = process_new_rss_articles(news, max_per_run=1)
    if added <= 0:
        return {"status": "none", "article_id": None, "message": "取り込める記事がありません"}
    NewsAggregator.get_news(force_refresh=True)
    try:
        from app.services.render_notifier import notify_render_cache_refresh

        notify_render_cache_refresh(reason=f"seed_one_added:{added}")
    except Exception:
        pass
    updated = NewsAggregator.get_news()
    new_id = updated[0].id if updated else None
    return {"status": "ok", "article_id": new_id}


@router.get("/api/article/seed-one")
async def api_seed_one_article():
    """RSSから1件読み込み、AI解説付きで記事を1件作る。作成した記事IDを返す（常にJSONで返す）"""
    if is_rss_and_ai_disabled():
        raise HTTPException(status_code=503, detail="この環境ではRSS取得・AI要約は無効です。")
    import asyncio
    import logging
    logger = logging.getLogger(__name__)
    try:
        loop = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _do_seed_one_sync),
            timeout=180.0,
        )
        return result
    except asyncio.TimeoutError:
        logger.warning("seed-one がタイムアウトしました")
        return {"status": "error", "article_id": None, "message": "処理がタイムアウトしました（3分）。RSSやOpenAIの応答が遅い可能性があります。もう一度お試しください。"}
    except Exception as e:
        logger.exception("seed-one でエラー")
        msg = str(e).strip() or "不明なエラー"
        return {"status": "error", "article_id": None, "message": f"処理に失敗しました: {msg}"}


def _do_force_add_one_sync():
    """RSS取得＋トレンド精査で1件選定＋AI処理（重い処理を同期的に実行・スレッドから呼ぶ用）"""
    from app.services.rss_service import fetch_rss_news
    from app.services.article_processor import process_rss_to_site_article, _rank_by_trending
    from app.services.explanation_cache import delete_cache
    from app.services.trends_service import fetch_trending_searches
    news = fetch_rss_news()
    if not news:
        return {"status": "error", "article_id": None, "message": "RSSから記事を取得できませんでした。フィードURLやネットワークを確認してください。"}
    trends = fetch_trending_searches()
    trend_keywords = [t.keyword for t in trends]
    ranked = _rank_by_trending(news, trend_keywords) if trend_keywords else news
    item = ranked[0]
    delete_cache(item.id)
    if process_rss_to_site_article(item, force=True):
        NewsAggregator.get_news(force_refresh=True)
        try:
            from app.services.render_notifier import notify_render_cache_refresh

            notify_render_cache_refresh(reason="force_add_one:1")
        except Exception:
            pass
        if NewsAggregator.get_article(item.id) is None:
            return {"status": "error", "article_id": None, "message": "記事の保存後に取得できませんでした。data フォルダの権限やDBを確認してください。"}
        return {"status": "ok", "article_id": item.id}
    return {"status": "error", "article_id": None, "message": "AI解説の生成に失敗しました。.env の AI_PROVIDER と API キー（OPENAI_API_KEY / GEMINI_API_KEY）を確認してください。"}


@router.get("/api/article/force-add-one")
async def api_force_add_one_article():
    """RSSの先頭1件を必ず1件取り込む。重い処理はスレッドで実行して固まらないようにする。"""
    if is_rss_and_ai_disabled():
        raise HTTPException(status_code=503, detail="この環境ではRSS取得・AI要約は無効です。")
    import asyncio
    import logging
    logger = logging.getLogger(__name__)
    try:
        loop = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _do_force_add_one_sync),
            timeout=180.0,
        )
        return result
    except asyncio.TimeoutError:
        logger.warning("force-add-one がタイムアウトしました")
        return {"status": "error", "article_id": None, "message": "処理がタイムアウトしました（3分）。RSSやOpenAIの応答が遅い可能性があります。もう一度お試しください。"}
    except Exception as e:
        logger.exception("force-add-one でエラー")
        msg = str(e).strip() or "不明なエラー"
        return {"status": "error", "article_id": None, "message": f"処理に失敗しました: {msg}"}
