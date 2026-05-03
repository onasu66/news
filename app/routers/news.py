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
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app.config import settings, is_rss_and_ai_disabled
from app.services.news_aggregator import NewsAggregator
from app.services.rss_service import NewsItem, sanitize_display_text
from app.services.article_cache import save_article
from app.services.ai_batch_service import generate_all_explanations
from app.services.ai_service import (
    explain_article_with_ai,
    get_image_url,
    PERSONAS,
    PERSONA_LOGIC_IDS,
    PERSONA_ENT_IDS,
)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
logger = logging.getLogger(__name__)

# 一覧ジャンルタブの data-cat と一致させる（DB/RSS の category がタブ6種以外だと「すべて」以外で消える）
_NEWS_TAB_CATEGORY_SET = frozenset({"国内", "国際", "テクノロジー", "政治・社会", "スポーツ", "エンタメ"})
_NEWS_TAB_OTHER = "その他"


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
    "セミナ": "/static/char-imgs/セミナ.png",
    "ヴォルテ・アセット": "/static/char-imgs/ヴぉるて.png",
    "カゲロウ": "/static/char-imgs/kagerou.png",
    "くらしあ": "/static/char-imgs/くらしあ.png",
    "アルシエル": "/static/char-imgs/あるしえる.png",
    "クロニクル": "/static/char-imgs/くろにくる.png",
    "ブレイズ": "/static/char-imgs/ぶれいず.png",
    "ノアフォール": "/static/char-imgs/ノアフォール.png",
    "そらみ": "/static/char-imgs/そらみ.png",
    "レガリア": "/static/char-imgs/れがりあ.png",
    "リュミエ": "/static/char-imgs/りゅみえ.png",
    "ジャスティア": "/static/char-imgs/ジャスティア.png",
    "観測体オメガ": "/static/char-imgs/オメガ.png",
    "ゼロ・カオス": "/static/char-imgs/ゼロカオス.png",
    "ミドルマン": "/static/char-imgs/ミドルマン.png",
}


def _persona_image_url(name: str | None) -> str:
    return PERSONA_IMAGE_MAP.get((name or "").strip(), "/static/site-imgs/ロゴ.png")


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
    """検索エンジン向け robots.txt"""
    site_url = _get_site_url(request)
    # keyword パラメータ付きページ（例: /?keyword=...）は重複になりやすいのでクロール抑制
    body = (
        f"User-agent: *\n"
        f"Allow: /\n"
        f"Disallow: /admin\n"
        f"Disallow: /confirm\n"
        f"Disallow: /saved\n"
        f"Disallow: /?keyword=\n"
        f"Disallow: /news?keyword=\n\n"
        f"Sitemap: {site_url}/sitemap.xml\n"
    )
    return Response(content=body, media_type="text/plain; charset=utf-8")


@router.get("/sitemap.xml")
async def sitemap_xml(request: Request):
    """SEO用 sitemap.xml（一覧は NewsAggregator キャッシュ利用で Firestore 読取を抑える）"""
    site_url = _get_site_url(request)
    articles = NewsAggregator.get_news()
    today = datetime.now().date().isoformat()
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
    ]
    for a in articles[:5000]:
        try:
            lastmod = a.published.date().isoformat() if getattr(a, "published", None) and hasattr(a.published, "date") else today
        except Exception:
            lastmod = today
        priority = "0.9" if getattr(a, "category", "") == "研究・論文" else "0.8"
        lines.append(f"  <url><loc>{site_url}/topic/{a.id}</loc><lastmod>{lastmod}</lastmod><changefreq>never</changefreq><priority>{priority}</priority></url>")
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
            return templates.TemplateResponse(
                "papers.html",
                {
                    "request": request,
                    "papers_by_category": [],
                    "pagination": {"page": 1, "per_page": 24, "total": 0, "total_pages": 1, "has_prev": False, "has_next": False},
                    "has_papers": False,
                    "site_url": _get_site_url(request),
                    "page": 1,
                    "recent_ai_news": [],
                    "top_recommendations": [],
                    "papers_breadcrumb_jsonld": None,
                    "papers_itemlist_jsonld": None,
                },
            )
    return Response(status_code=405)


@router.api_route("/news", methods=["GET", "POST", "HEAD", "OPTIONS"])
async def news_index(request: Request, page: int = 1, keyword: str = ""):
    """ニュース一覧（ジャンル別）。keyword なし時は論文除く全件を1ページ表示（『すべて』に全ニュース）。"""
    if request.method == "POST":
        return {"message": "ok"}
    if request.method == "OPTIONS":
        return Response(status_code=200)
    from app.services.news_aggregator import ITEMS_PER_PAGE
    keyword = (keyword or "").strip()
    all_news = [a for a in NewsAggregator.get_news() if (a.category or "") != "研究・論文"]
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
    else:
        # キーワードなし: 一覧は全件（get_news が返す範囲内）。おすすめも同じリストの先頭3件。
        total = len(all_news)
        page_items = all_news
        by_cat: dict[str, list] = {}
        for a in all_news:
            t = _news_tab_filter_category(a)
            by_cat.setdefault(t, []).append(a)
        news_category_order = _news_tab_category_order_for(by_cat)
        news_by_category = [(c, by_cat.get(c, [])) for c in news_category_order]
        news_tab_filter_cat = {a.id: _news_tab_filter_category(a) for a in all_news}
        pagination = {
            "page": 1,
            "per_page": total or 1,
            "total": total,
            "total_pages": 1,
            "has_prev": False,
            "has_next": False,
        }
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
    if not keyword:
        try:
            top_recommendations = list(all_news[:3])
        except Exception:
            top_recommendations = []
    site_url = _get_site_url(request)
    og_image = "https://picsum.photos/1200/630"
    flat_news = [it for _, items in news_by_category for it in items]
    news_breadcrumb_jsonld = _build_breadcrumb_jsonld(
        [("ホーム", f"{site_url}/"), ("AIニュースアーカイブ", f"{site_url}/news")]
    )
    news_itemlist_jsonld = _build_itemlist_jsonld(
        page_name="AIニュースアーカイブ一覧",
        site_url=site_url,
        items=flat_news,
    )
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
            "news_breadcrumb_jsonld": news_breadcrumb_jsonld,
            "news_itemlist_jsonld": news_itemlist_jsonld,
        },
    )


@router.get("/api/news/page")
async def api_news_page(page: int = 1, keyword: str = ""):
    """無限スクロール用：キーワード検索時のみページ分割。通常一覧はSSRで全件のため追加HTMLなし。"""
    from app.services.news_aggregator import ITEMS_PER_PAGE
    keyword = (keyword or "").strip()
    all_news = [a for a in NewsAggregator.get_news() if (a.category or "") != "研究・論文"]
    if not keyword:
        return {"html": "", "page": max(1, page), "total_pages": 1}
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
        img_src = item.image_url or "https://picsum.photos/400/225"
        cards_html += f'''<article class="news-card animate-fade-in" data-category="{tab_cat_safe}">
<a href="/topic/{item.id}" class="news-card-link">
<div class="news-card-body">
<div class="news-card-meta"><span class="news-card-time">🕒 {pub}</span><span class="news-card-source">{source_safe}</span></div>
<h3 class="news-title">{title_safe}</h3>
<p class="news-summary-line">👀 {summary_safe}{ellipsis}</p>
<div class="news-card-footer"><span class="news-card-ai">✍ AIが解説</span><span class="news-badge">AI解説</span></div>
</div>
<div class="news-card-image"><img src="{img_src}" alt="{title_safe}" loading="lazy" onerror="this.src='https://picsum.photos/seed/{item.id}/400/225'"><span class="news-card-category">{cat_safe}</span></div>
</a></article>'''
    return {"html": cards_html, "page": page, "total_pages": total_pages}


@router.get("/api/papers/page")
async def api_papers_page(page: int = 1):
    """論文ページ用・無限スクロール：指定ページの論文カードHTMLを返す"""
    papers_by_category, pagination = NewsAggregator.get_papers_by_category(page=page)
    import html as html_mod
    cards_html = ""
    for domain, items in papers_by_category:
        _attach_paper_related_tags(items)
        for item in items:
            _ensure_japanese(item)
            if not item.image_url:
                item.image_url = get_image_url(item.id, 400, 225)
            elif not item.image_url.startswith("http"):
                item.image_url = get_image_url(item.image_url, 400, 225)
            pub = item.published.strftime('%m/%d %H:%M') if item.published else ''
            title_safe = html_mod.escape(item.title or "")
            raw_summary = item.summary or ""
            summary_safe = html_mod.escape(raw_summary[:80])
            domain_safe = html_mod.escape(domain or "")
            source_safe = html_mod.escape(item.source or "")
            ellipsis = "..." if len(raw_summary) > 80 else ""
            img_src = item.image_url or "https://picsum.photos/400/225"
            cards_html += f'''<article class="news-card animate-fade-in" data-category="{domain_safe}">
<a href="/topic/{item.id}" class="news-card-link">
<div class="news-card-body">
<div class="news-card-meta"><span class="news-card-time">🕒 {pub}</span><span class="news-card-source">{source_safe}</span></div>
<h3 class="news-title">{title_safe}</h3>
<p class="news-summary-line">👀 {summary_safe}{ellipsis}</p>
<div class="news-card-footer"><span class="news-card-ai">✍ AIが解説</span><span class="news-badge">AI解説</span></div>
</div>
<div class="news-card-image"><img src="{img_src}" alt="{title_safe}" loading="lazy" onerror="this.src='https://picsum.photos/seed/{item.id}/400/225'"><span class="news-card-category">{domain_safe}</span></div>
</a></article>'''
    return {"html": cards_html, "page": pagination["page"], "total_pages": pagination["total_pages"]}


@router.get("/papers")
async def papers_legacy_redirect(request: Request):
    """旧URL互換：トップ（論文一覧）へ統合"""
    q = request.url.query
    return RedirectResponse(url=("/?" + q) if q else "/", status_code=301)


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
    return templates.TemplateResponse("trend.html", {"request": request, "articles": top_articles, "trends": trends})


@router.get("/ai", response_class=HTMLResponse)
async def ai_page(request: Request):
    """AIページ：おすすめ・昨日のメモ・人格コメント"""
    all_news = NewsAggregator.get_news()
    recommended = all_news[:6]
    for item in recommended:
        _ensure_japanese(item)
        if not item.image_url:
            item.image_url = get_image_url(item.id, 400, 225)
        elif not item.image_url.startswith("http"):
            item.image_url = get_image_url(item.image_url, 400, 225)
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
    return templates.TemplateResponse(
        "ai.html",
        {
            "request": request,
            "recommended": recommended,
            "ai_memo": ai_memo,
            "ai_personas": ai_personas,
        },
    )


@router.get("/about", response_class=HTMLResponse)
async def about_page(request: Request):
    """運営者情報ページ"""
    return templates.TemplateResponse("about.html", {"request": request})


@router.get("/authors", response_class=HTMLResponse)
async def authors_page(request: Request):
    """著者情報ページ"""
    return templates.TemplateResponse("authors.html", {"request": request})


@router.get("/personas", response_class=HTMLResponse)
async def personas_page(request: Request):
    """14キャラクター紹介ページ"""
    personas = [_build_persona_view(p) for p in PERSONAS]
    return templates.TemplateResponse(
        "personas.html",
        {
            "request": request,
            "personas": personas,
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
    return templates.TemplateResponse(
        "persona_detail.html",
        {
            "request": request,
            "persona": persona,
            "other_personas": other_personas,
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
    return templates.TemplateResponse("search.html", {"request": request, "query": q, "results": results})


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
        elif item.image_url and not item.image_url.startswith("http"):
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
    ai_authors = [{"@type": "Person", "name": p.get("name", "")} for p in PERSONAS]
    contributors = []
    for pid in (display_persona_ids or []):
        try:
            p = PERSONAS[int(pid)]
            contributors.append({"@type": "Person", "name": p.get("name", "")})
        except Exception:
            continue
    return {
        "@context": "https://schema.org",
        "@type": article_type,
        "mainEntityOfPage": {"@type": "WebPage", "@id": article_url},
        "headline": (item.title or "").strip(),
        "description": (meta_desc or "").strip(),
        "datePublished": _iso_date(getattr(item, "published", None)),
        "dateModified": _iso_date(getattr(item, "published", None)),
        "author": ai_authors,
        "contributor": contributors,
        "publisher": {"@type": "Organization", "name": "知リポAI", "url": site_url},
        "image": [og_image] if og_image else [],
        "articleSection": item.category or "ニュース",
        "isAccessibleForFree": True,
    }


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
                "url": f"{site_url.rstrip('/')}/topic/{it.id}",
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

    # Firestore の場合、関連タグだけバルクリードする
    try:
        from app.services.firestore_store import use_firestore, firestore_get_related_tags_bulk

        firestore_mode = bool(use_firestore())
    except Exception:
        firestore_mode = False
        firestore_get_related_tags_bulk = None

    tags_by_id: dict[str, list[str]] = {}
    if firestore_mode and firestore_get_related_tags_bulk:
        tags_by_id = firestore_get_related_tags_bulk(missing_ids, max_tags_per_article=3)
    else:
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

    論文は get_news（Firestore 時は articles 全件メモリ）ではなく、研究・論文＋解説付き専用クエリで取得する。
    ページネーションは使わず PAPERS_LIST_MAX 件まで1ページで描画する。
    """
    from app.services.article_cache import load_papers_for_site_list
    from app.services.news_aggregator import SOURCE_TO_PAPER_DOMAIN, PAPER_DOMAIN_ORDER

    # ── 全論文（DB 専用クエリ。Firestore は全件スナップショット＋論文専用経路） ──
    all_papers = load_papers_for_site_list()
    all_news = NewsAggregator.get_news()

    # ── ドメイン分類（全論文に適用） ─────────────────────────────────────────
    by_domain: dict[str, list] = {}
    for item in all_papers:
        domain = SOURCE_TO_PAPER_DOMAIN.get(item.source, "総合科学")
        by_domain.setdefault(domain, []).append(item)

    # 表示順（データが存在するドメインのみ）
    paper_domains = [d for d in PAPER_DOMAIN_ORDER if d in by_domain]
    papers_by_category = [(d, by_domain[d]) for d in paper_domains]

    # ── 画像 / 関連タグ補完 ───────────────────────────────────────────────────
    for _, items in papers_by_category:
        _attach_paper_related_tags(items)
        for item in items:
            _ensure_japanese(item)
            if not item.image_url:
                item.image_url = get_image_url(item.id, 400, 225)
            elif item.image_url and not item.image_url.startswith("http"):
                item.image_url = get_image_url(item.image_url, 400, 225)

    # ── ページネーション: 全件を1ページに（無限スクロール不使用） ──────────
    total = len(all_papers)
    pagination = {
        "page": 1,
        "per_page": total or 1,
        "total": total,
        "total_pages": 1,
        "has_prev": False,
        "has_next": False,
    }

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
    has_papers = bool(all_papers)

    # おすすめは一覧と同じ all_papers の先頭3件（別取得ロジックなし）
    top_recommendations: list = []
    try:
        top_recommendations = all_papers[:3]
    except Exception:
        top_recommendations = []

    recent_ai_news: list[dict] = []
    try:
        non_papers = [x for x in all_news[:120] if x.category != "研究・論文"][:10]
        if non_papers:
            from app.services.explanation_cache import get_cached_many
            cached_map = get_cached_many([x.id for x in non_papers])
            for it in non_papers:
                c = cached_map.get(it.id, {}) if isinstance(cached_map, dict) else {}
                pids = c.get("display_persona_ids") if isinstance(c, dict) else []
                emojis: list[str] = []
                if isinstance(pids, list):
                    for pid in pids[:3]:
                        try:
                            emojis.append(PERSONAS[int(pid)]["emoji"])
                        except Exception:
                            continue
                recent_ai_news.append({"id": it.id, "title": it.title or "", "emojis": emojis})
    except Exception:
        recent_ai_news = []

    return templates.TemplateResponse(
        "papers.html",
        {
            "request": request,
            "papers_by_category": papers_by_category,
            "paper_domains": paper_domains,
            "pagination": pagination,
            "has_papers": has_papers,
            "site_url": site_url,
            "page": 1,
            "recent_ai_news": recent_ai_news,
            "top_recommendations": top_recommendations,
            "papers_breadcrumb_jsonld": papers_breadcrumb_jsonld,
            "papers_itemlist_jsonld": papers_itemlist_jsonld,
        },
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


@router.get("/topic/{topic_id}", response_class=HTMLResponse)
async def topic_detail(request: Request, topic_id: str):
    """トピック詳細（URL: /topic/○○）・AI解説・SEO向け本文"""
    from app.services.explanation_cache import get_cached

    item = NewsAggregator.get_article(topic_id)
    if not item:
        raise HTTPException(status_code=404, detail="記事が見つかりません")
    _ensure_japanese(item)
    image_url = item.image_url or get_image_url(item.id, 800, 450)
    if image_url and not image_url.startswith("http"):
        image_url = get_image_url(image_url, 800, 450)
    site_url = _get_site_url(request)
    article_url = f"{site_url}/topic/{topic_id}"
    og_image = image_url if (image_url or "").startswith("http") else f"{site_url}{image_url}" if image_url else ""
    if not og_image:
        og_image = get_image_url(item.id, 1200, 630)
    cached = get_cached(topic_id)
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
        personas_data = [str(x) if x is not None else "" for x in cached_personas[:3]]
    else:
        raw_personas = cached.get("personas", []) if cached else []
        all_personas_data = raw_personas if isinstance(raw_personas, list) else []
        import random as _rnd
        logic_ids = list(PERSONA_LOGIC_IDS)
        ent_ids = list(PERSONA_ENT_IDS)
        if len(logic_ids) >= 2 and len(ent_ids) >= 1:
            pick_logic = _rnd.sample(logic_ids, 2)
            pick_ent = _rnd.sample(ent_ids, 1)
            display_indices = pick_logic + pick_ent
            _rnd.shuffle(display_indices)
        else:
            display_indices = list(range(min(3, len(PERSONAS))))
        display_personas = [{**PERSONAS[i], "image_url": _persona_image_url(PERSONAS[i].get("name"))} for i in display_indices]
        personas_data = [
            str(all_personas_data[i]) if i < len(all_personas_data) and all_personas_data[i] is not None else ""
            for i in display_indices
        ]
        display_persona_ids = display_indices
    # Firestore の Decimal 等で Jinja |tojson が落ちるのを防ぐ＋型崩れを正規化
    ps_wrapped = _json_safe_for_template(personas_data)
    if isinstance(ps_wrapped, list):
        personas_data = [str(x) if x is not None else "" for x in ps_wrapped]
    quick_understand = _sanitize_quick_understand_for_page(cached.get("quick_understand") if cached else None)
    vote_data = _sanitize_vote_data_for_page(cached.get("vote_data") if cached else None)
    paper_graph = _sanitize_paper_graph_for_page(cached.get("paper_graph") if cached else None)
    paper_quiz = _sanitize_paper_quiz_for_page(cached.get("paper_quiz") if cached else None)
    # Firestore/過去データ互換:
    # 片方にしか入っていない場合でも、クイズ/投票のどちらにも表示できるようにする
    if (not vote_data) and paper_quiz and paper_quiz.get("options"):
        vote_data = dict(paper_quiz)
    if (not paper_quiz or not paper_quiz.get("options")) and vote_data and vote_data.get("options"):
        paper_quiz = dict(vote_data)
    deep_insights = _sanitize_deep_insights_for_page(cached.get("deep_insights") if cached else None)
    body_html = _blocks_to_html(blocks) if blocks else ""
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
        if a.id == topic_id:
            if i + 1 < len(all_news):
                next_article = all_news[i + 1]
            if i > 0:
                prev_article = all_news[i - 1]
            break

    related = [a for a in all_news if a.category == item.category and a.id != topic_id][:4]
    import random as _rnd
    other_cat = [a for a in all_news if a.category != item.category and a.id != topic_id]
    # 論文のみ・単一ジャンルだけ等で「別カテゴリ」が空だと欄が消えるため、同カテゴリ以外が無ければ全件から補完
    pool = other_cat if other_cat else [a for a in all_news if a.id != topic_id]
    ai_recommended = _rnd.sample(pool, min(3, len(pool))) if pool else []

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
    _article_cat = (getattr(item, "category", None) or "").strip()
    mobile_nav_papers_highlight = _article_cat == "研究・論文"
    mobile_nav_news_highlight = not mobile_nav_papers_highlight
    all_personas_enriched = [{**p, "image_url": _persona_image_url(p.get("name"))} for p in PERSONAS]

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
            "article_url": article_url,
            "og_image": og_image,
            "blocks": blocks,
            "personas_data": personas_data,
            "body_html": body_html,
            "short_summary": short_summary,
            "article_lead_html": article_lead_html,
            "show_quick_points": show_quick_points,
            "quick_rows": quick_rows,
            "meta_description": meta_desc,
            "next_article": next_article,
            "prev_article": prev_article,
            "related_articles": related,
            "same_category_articles": related,
            "ai_recommended": ai_recommended,
            "quick_understand": quick_understand,
            "vote_data": vote_data,
            "paper_graph": paper_graph,
            "paper_quiz": paper_quiz,
            "deep_insights": deep_insights,
            "readers_now": readers_now,
            "published_text": published_text,
            "copy_blurb": copy_blurb,
            "article_jsonld": article_jsonld,
            "mobile_nav_papers_highlight": mobile_nav_papers_highlight,
            "mobile_nav_news_highlight": mobile_nav_news_highlight,
            "midorman_image_url": _persona_image_url("ミドルマン"),
        }
    )


@router.get("/article/{article_id}", response_class=HTMLResponse)
async def article_detail(request: Request, article_id: str):
    """旧URL: /topic/ へリダイレクト"""
    item = NewsAggregator.get_article(article_id)
    if not item:
        raise HTTPException(status_code=404, detail="記事が見つかりません")
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/topic/{article_id}", status_code=301)


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
    item = NewsAggregator.get_article(article_id)
    if not item:
        raise HTTPException(status_code=404, detail="記事が見つかりません")
    content = f"{item.title}\n\n{item.summary}"
    explanation = explain_article_with_ai(item.title, content)
    return {"explanation": explanation}


@router.get("/api/article/{article_id}/explain-inline")
async def api_explain_inline(article_id: str):
    """記事本文と解説が交互に入った構造で取得（従来API・互換用）"""
    item = NewsAggregator.get_article(article_id)
    if not item:
        raise HTTPException(status_code=404, detail="記事が見つかりません")
    data = generate_all_explanations(article_id, item.title, f"{item.title}\n\n{item.summary}", category=item.category)
    return {"blocks": _sanitize_blocks(data["blocks"])}


@router.get("/api/article/{article_id}/explanations")
async def api_all_explanations(article_id: str):
    """ミドルマン解説＋人格の意見を一括取得（キャッシュ優先・表示用3人分のみ生成）"""
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
        has_key = bool(getattr(settings, "OPENAI_API_KEY", ""))
    except Exception:
        has_key = False

    return {
        "articles_in_db": displayable,
        "ai_processed": processed_count,
        "displayable": displayable,
        "openai_key_set": has_key,
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
    if not getattr(settings, "ADMIN_SECRET", ""):
        return False
    if x_admin_secret and x_admin_secret.strip() == settings.ADMIN_SECRET:
        return True
    return request.session.get("admin") is True


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
    """手動記事をAIで生成して保存（同期・スレッド実行用）。generate_all_explanations 内で save_cache 済み"""
    article_id = "manual-" + uuid.uuid4().hex[:16]
    content = sanitize_display_text(f"{title}\n\n{summary}")[:20000]
    data = generate_all_explanations(article_id, title, content, category="総合")
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
    NewsAggregator.get_news(force_refresh=not is_rss_and_ai_disabled())
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


@router.post("/api/admin/sync-meta")
async def api_admin_sync_meta(
    request: Request,
    x_admin_secret: str | None = Header(None, alias="X-Admin-Secret"),
):
    """
    Firestore の _meta/cache（表示対象の記事ID一覧）を explanations コレクションと同期する。
    「記事は8件あるが表示は3件」のとき、explanations に8件あれば同期後に8件表示される。
    管理者のみ。実行後に一覧キャッシュを強制更新する。
    """
    if not _is_admin(request, x_admin_secret):
        raise HTTPException(status_code=403, detail="管理者のみ利用できます")
    try:
        from app.services.firestore_store import use_firestore, firestore_sync_meta_from_explanations
        from app.services.explanation_cache import invalidate_ids_cache
    except ImportError:
        raise HTTPException(status_code=501, detail="Firestore 未使用のためこのAPIは利用できません")
    if not use_firestore():
        return {"status": "ok", "synced": 0, "message": "Firestore 未使用のためスキップしました"}
    synced = firestore_sync_meta_from_explanations()
    invalidate_ids_cache()
    NewsAggregator.get_news(force_refresh=True)
    return {"status": "ok", "synced": synced}


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
        if NewsAggregator.get_article(item.id) is None:
            return {"status": "error", "article_id": None, "message": "記事の保存後に取得できませんでした。data フォルダの権限やDBを確認してください。"}
        return {"status": "ok", "article_id": item.id}
    return {"status": "error", "article_id": None, "message": "AI解説の生成に失敗しました。.env の OPENAI_API_KEY を確認し、利用可能なモデル（OPENAI_MODEL）を指定してください。"}


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
