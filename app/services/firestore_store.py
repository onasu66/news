"""Firestore ストア - 記事・解説を Firestore に永続化（Render 等での永続化対応）。
無料枠（読 5万/日・書 2万/日）を考慮し、cached_article_ids はメタ1ドキュメントで管理・load_all は limit 付き。"""
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

from google.api_core.exceptions import FailedPrecondition

logger = logging.getLogger(__name__)

try:
    from app.config import settings
    _FIREBASE_JSON = (getattr(settings, "FIREBASE_SERVICE_ACCOUNT_JSON", "") or "").strip()
except Exception:
    _FIREBASE_JSON = ""

# credentials/ の JSON ファイル（環境変数未設定時のローカル用）
_CREDENTIALS_PATH = Path(__file__).resolve().parent.parent.parent / "credentials" / "firebase-service-account.json"

_client = None


def _load_credential_dict():
    """サービスアカウント認証情報を取得（env優先、次に credentials ファイル）。JSON が不正なら None。"""
    if _FIREBASE_JSON:
        try:
            return json.loads(_FIREBASE_JSON)
        except json.JSONDecodeError as e:
            logger.warning("FIREBASE_SERVICE_ACCOUNT_JSON の JSON が不正です: %s", e)
            return None
    if _CREDENTIALS_PATH.exists():
        try:
            with open(_CREDENTIALS_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("credentials ファイルの読み込みに失敗しました: %s", e)
            return None
    return None


def _get_client():
    """Firestore クライアントを取得（遅延初期化）"""
    global _client
    if _client is not None:
        return _client
    cred_dict = _load_credential_dict()
    if not cred_dict:
        raise RuntimeError(
            "Firebase の認証情報がありません。FIREBASE_SERVICE_ACCOUNT_JSON 環境変数か "
            "credentials/firebase-service-account.json を設定してください。"
        )
    import firebase_admin
    from firebase_admin import credentials, firestore
    try:
        # すでにデフォルトアプリが初期化されていればそれを使う
        firebase_admin.get_app()
    except ValueError:
        # 別スレッドとの競合で initialize_app が二重に呼ばれても問題ないように二重ガード
        try:
            firebase_admin.initialize_app(credentials.Certificate(cred_dict))
        except ValueError:
            # ここに来るのは「今この瞬間に別スレッドが initialize 済み」の場合なので無視して続行
            pass
    _client = firestore.client()
    return _client


def _server_timestamp():
    """Firestore サーバータイムスタンプ"""
    from google.cloud.firestore_v1 import SERVER_TIMESTAMP
    return SERVER_TIMESTAMP


def _articles_collection():
    return _get_client().collection("articles")


def _explanations_collection():
    return _get_client().collection("explanations")


def _meta_doc():
    """メタ情報用ドキュメント（cached_article_ids 等）。読み取り回数削減のため1ドキュメントで管理"""
    return _get_client().collection("_meta").document("cache")


# 一覧取得の上限（無料枠 5万読/日 を考慮。過剰読取を抑える）
_LOAD_ALL_LIMIT = 800

# --- articles ---
def firestore_load_by_id(article_id: str):
    from .rss_service import NewsItem, sanitize_display_text
    doc = _articles_collection().document(article_id).get()
    if not doc.exists:
        return None
    d = doc.to_dict()
    try:
        pub = datetime.fromisoformat(d.get("published", "")) if d.get("published") else datetime.now()
    except Exception:
        pub = datetime.now()
    added_at_raw = d.get("added_at")
    added_at = added_at_raw.replace(tzinfo=None) if hasattr(added_at_raw, "replace") else None
    return NewsItem(
        id=doc.id,
        title=d.get("title", ""),
        link=d.get("link", ""),
        summary=sanitize_display_text(d.get("summary") or ""),
        published=pub,
        source=d.get("source", ""),
        category=d.get("category", "総合"),
        image_url=d.get("image_url"),
        added_at=added_at,
    )


def firestore_load_all():
    """保存済み記事を新しい順で取得。読み取り数削減のため上限あり"""
    from .rss_service import NewsItem, sanitize_display_text
    items = []
    for doc in _articles_collection().order_by("added_at", direction="DESCENDING").limit(_LOAD_ALL_LIMIT).stream():
        d = doc.to_dict()
        try:
            pub = datetime.fromisoformat(d.get("published", "")) if d.get("published") else datetime.now()
        except Exception:
            pub = datetime.now()
        added_at_raw = d.get("added_at")
        added_at = added_at_raw.replace(tzinfo=None) if hasattr(added_at_raw, "replace") else None
        items.append(NewsItem(
            id=doc.id,
            title=d.get("title", ""),
            link=d.get("link", ""),
            summary=sanitize_display_text(d.get("summary") or ""),
            published=pub,
            source=d.get("source", ""),
            category=d.get("category", "総合"),
            image_url=d.get("image_url"),
            added_at=added_at,
        ))
    return items


def _firestore_article_doc_to_item(doc_id: str, d: dict) -> "NewsItem":
    from .rss_service import NewsItem, sanitize_display_text

    try:
        pub = datetime.fromisoformat(d.get("published", "")) if d.get("published") else datetime.now()
    except Exception:
        pub = datetime.now()
    added_at_raw = d.get("added_at")
    added_at = added_at_raw.replace(tzinfo=None) if hasattr(added_at_raw, "replace") else None
    return NewsItem(
        id=doc_id,
        title=d.get("title", ""),
        link=d.get("link", ""),
        summary=sanitize_display_text(d.get("summary") or ""),
        published=pub,
        source=d.get("source", ""),
        category=d.get("category", "研究・論文"),
        image_url=d.get("image_url"),
        added_at=added_at,
    )


def _firestore_papers_fallback_scan(target: int) -> list:
    """category+has_explanation+published の複合インデックスが無いとき、added_at 新しい順に広く読んで論文だけ拾う。"""
    cap = max(1, min(int(target), 50000))
    scan = min(max(cap * 8, 4000), 50000)
    out: list = []
    for doc in _articles_collection().order_by("added_at", direction="DESCENDING").limit(scan).stream():
        d = doc.to_dict() or {}
        if d.get("category") != "研究・論文" or not d.get("has_explanation"):
            continue
        out.append(_firestore_article_doc_to_item(doc.id, d))
        if len(out) >= cap:
            break
    return out


def firestore_load_all_papers_for_site_list(limit: int = 20000) -> list:
    """論文トップ SSR 用: category=研究・論文かつ has_explanation のみを published 降順で取得。
    load_all の上位800件（ニュース混在）では落ちる論文を拾う。上限は無料枠のため cap あり。"""
    cap = max(1, min(int(limit), 50000))
    items: list = []
    q = (
        _articles_collection()
        .where("category", "==", "研究・論文")
        .where("has_explanation", "==", True)
        .order_by("published", direction="DESCENDING")
        .limit(cap)
    )
    try:
        for doc in q.stream():
            d = doc.to_dict() or {}
            items.append(_firestore_article_doc_to_item(doc.id, d))
        return items
    except FailedPrecondition as e:
        logger.warning(
            "firestore_load_all_papers_for_site_list: 複合インデックス未作成のため added_at スキャンにフォールバックします（%s）",
            e,
        )
        return _firestore_papers_fallback_scan(cap)


def firestore_save_articles_batch(items) -> int:
    """記事を一括保存。読み取り削減のため get せず set（上書き含む）。戻り値は保存試行数"""
    col = _articles_collection()
    count = 0
    for item in items:
        try:
            data = {
                "title": item.title,
                "link": item.link,
                "summary": (item.summary or "")[:4000],
                "published": item.published.isoformat() if hasattr(item.published, "isoformat") else str(item.published),
                "source": item.source or "",
                "category": item.category or "総合",
                "image_url": item.image_url,
                "added_at": _server_timestamp(),
            }
            col.document(item.id).set(data)
            count += 1
        except Exception:
            pass
    return count


def firestore_save_article(item) -> bool:
    try:
        _articles_collection().document(item.id).set({
            "title": item.title,
            "link": item.link,
            "summary": (item.summary or "")[:4000],
            "published": item.published.isoformat() if hasattr(item.published, "isoformat") else str(item.published),
            "source": item.source or "",
            "category": item.category or "総合",
            "image_url": item.image_url,
            "added_at": _server_timestamp(),
        })
        return True
    except Exception:
        return False


def firestore_delete_article(article_id: str) -> bool:
    ref = _articles_collection().document(article_id)
    if ref.get().exists:
        ref.delete()
        return True
    return False


# --- explanations ---
def _is_bad_fallback_cache(blocks: list) -> bool:
    if not blocks or len(blocks) != 2:
        return False
    types = [b.get("type") for b in blocks if isinstance(b, dict)]
    if types != ["text", "explain"]:
        return False
    explain_content = next((b.get("content", "") for b in blocks if isinstance(b, dict) and b.get("type") == "explain"), "")
    bad_phrases = ("構造化に失敗", "通常の解説を表示", "しばらくしてから再度")
    return any(p in explain_content for p in bad_phrases)


def _rebuild_cached_article_ids_meta() -> set:
    """explanations をスキャンして _meta/cache の ids を書き直す（初回・不整合修復用）"""
    ids = []
    for doc in _explanations_collection().limit(2000).stream():
        ids.append(doc.id)
    try:
        _meta_doc().set({"ids": ids, "updated_at": _server_timestamp()})
    except Exception:
        pass
    return set(ids)


def firestore_get_cached_article_ids() -> set:
    """AI処理済み記事ID。メタドキュメント1読で返す（explanations 全件ストリーム廃止・読み取り削減）"""
    meta = _meta_doc().get()
    if meta.exists:
        ids = meta.to_dict().get("ids") or []
        if ids:
            return set(ids)
        # メタはあるのに ids が空: コンソール編集・古い不整合で「記事があるのに一覧0件」になる。
        # explanations に1件でもあればフルスキャンしてメタを修復する。
        try:
            if not any(_explanations_collection().limit(1).stream()):
                return set()
        except Exception:
            return set()
        logger.warning(
            "_meta/cache の ids が空だが explanations にデータがあります。メタを explanations から再構築します。"
        )
        return _rebuild_cached_article_ids_meta()
    return _rebuild_cached_article_ids_meta()


def firestore_get_related_tags_bulk(article_ids: list[str], *, max_tags_per_article: int = 3) -> dict[str, list[str]]:
    """
    複数 article_id に対して explanations/doc を一括取得し、
    paper_graph.related_tags だけを抜き出して返す。

    目的: /papers 一覧で get_cached() をカード数ぶん直列実行するのではなく、
    Firestore のバッチ読取（get_all）で待ち時間を短縮する。
    """
    if not article_ids:
        return {}

    client = _get_client()
    refs = [_explanations_collection().document(aid) for aid in article_ids]
    results: dict[str, list[str]] = {}

    # Firestore client has get_all(); ない場合はフォールバックでループ
    try:
        docs = client.get_all(refs)
    except Exception:
        docs = []
        for ref in refs:
            try:
                docs.append(ref.get())
            except Exception:
                pass

    for doc in docs:
        try:
            if not doc.exists:
                continue
            d = doc.to_dict() or {}
            pg = d.get("paper_graph")
            if isinstance(pg, str):
                try:
                    pg = json.loads(pg)
                except Exception:
                    pg = None
            if not isinstance(pg, dict):
                continue
            raw_tags = pg.get("related_tags", [])
            if not isinstance(raw_tags, list):
                continue
            tags = [str(t).strip() for t in raw_tags if str(t).strip()][:max_tags_per_article]
            results[doc.id] = tags
        except Exception:
            continue

    return results


def firestore_query_papers_page(page: int, per_page: int) -> tuple[list["NewsItem"], int]:
    """
    /papers 用: has_explanation=True & category=研究・論文 を published 降順でページング取得。

    目的: /papers 初回表示時に get_news() 経由で load_all(最大2000件) を回さない。
    """
    from .rss_service import NewsItem, sanitize_display_text

    page = max(1, int(page or 1))
    per_page = max(1, int(per_page or 1))
    start = (page - 1) * per_page

    q = (
        _articles_collection()
        .where("category", "==", "研究・論文")
        .where("has_explanation", "==", True)
        .order_by("published", direction="DESCENDING")
        .offset(start)
        .limit(per_page)
    )

    items: list[NewsItem] = []
    for doc in q.stream():
        d = doc.to_dict() or {}
        try:
            pub = datetime.fromisoformat(d.get("published", "")) if d.get("published") else datetime.now()
        except Exception:
            pub = datetime.now()
        items.append(
            NewsItem(
                id=doc.id,
                title=d.get("title", ""),
                link=d.get("link", ""),
                summary=sanitize_display_text(d.get("summary") or ""),
                published=pub,
                source=d.get("source", ""),
                category=d.get("category", "研究・論文"),
                image_url=d.get("image_url"),
            )
        )

    # total_count は count() が使えればそれ、ダメなら前半だけフォールバック（表示上限の範囲）
    total_count = 0
    try:
        base = (
            _articles_collection()
            .where("category", "==", "研究・論文")
            .where("has_explanation", "==", True)
        )
        # Firestore の aggregation API が使える場合
        agg = base.count().get()
        # 返却形式はバージョン差があるので複数パターン対応
        total_count = int(getattr(agg, "value", None) or agg[0].value or agg[0].get("count") or 0)
    except Exception:
        try:
            # 最悪: 一旦ページ上限分だけ読む（papers 自体はニュース全体より少ない想定）
            total_count = 0
            for _ in (
                _articles_collection()
                .where("category", "==", "研究・論文")
                .where("has_explanation", "==", True)
                .limit(per_page * 100)  # かなり広め（通常は収まる想定）
                .stream()
            ):
                total_count += 1
        except Exception:
            total_count = len(items)

    return items, total_count


def firestore_query_news_page(page: int, per_page: int) -> tuple[list["NewsItem"], int]:
    """
    /news 用: has_explanation=True かつ category!=研究・論文 を published 降順でページング取得。
    /news 初回表示時に get_news() -> load_all() を避ける。
    """
    from .rss_service import NewsItem, sanitize_display_text

    page = max(1, int(page or 1))
    per_page = max(1, int(per_page or 1))
    start = (page - 1) * per_page

    q = (
        _articles_collection()
        .where("has_explanation", "==", True)
        .where("category", "!=", "研究・論文")
        .order_by("category")
        .order_by("published", direction="DESCENDING")
        .offset(start)
        .limit(per_page)
    )

    items: list[NewsItem] = []
    for doc in q.stream():
        d = doc.to_dict() or {}
        try:
            pub = datetime.fromisoformat(d.get("published", "")) if d.get("published") else datetime.now()
        except Exception:
            pub = datetime.now()
        items.append(
            NewsItem(
                id=doc.id,
                title=d.get("title", ""),
                link=d.get("link", ""),
                summary=sanitize_display_text(d.get("summary") or ""),
                published=pub,
                source=d.get("source", ""),
                category=d.get("category", "総合"),
                image_url=d.get("image_url"),
            )
        )

    total_count = 0
    try:
        base = (
            _articles_collection()
            .where("has_explanation", "==", True)
            .where("category", "!=", "研究・論文")
        )
        agg = base.count().get()
        total_count = int(getattr(agg, "value", None) or agg[0].value or agg[0].get("count") or 0)
    except Exception:
        try:
            total_count = 0
            for _ in (
                _articles_collection()
                .where("has_explanation", "==", True)
                .where("category", "!=", "研究・論文")
                .limit(per_page * 100)
                .stream()
            ):
                total_count += 1
        except Exception:
            total_count = len(items)

    return items, total_count


def firestore_get_cached(article_id: str) -> Optional[dict]:
    doc = _explanations_collection().document(article_id).get()
    if not doc.exists:
        return None
    d = doc.to_dict()
    try:
        blocks = json.loads(d.get("inline_blocks", "[]"))
    except Exception:
        logger.warning("firestore_get_cached: inline_blocks が不正 article_id=%s", article_id)
        return None
    if _is_bad_fallback_cache(blocks):
        return None
    # 新形式: 表示用3人分のみ保存されている場合
    if "display_persona_ids" in d and isinstance(d["display_persona_ids"], list) and len(d["display_persona_ids"]) == 3 and "personas" in d and isinstance(d["personas"], list) and len(d["personas"]) == 3:
        result = {"blocks": blocks, "personas": list(d["personas"]), "display_persona_ids": list(d["display_persona_ids"])}
    else:
        if "personas" in d and isinstance(d["personas"], list):
            personas = (d["personas"] + [""] * 14)[:14]
        else:
            personas = [d.get(f"persona_{i}", "") or "" for i in range(14)]
        result = {"blocks": blocks, "personas": personas}
    def _merge_optional_dict_field(key: str) -> None:
        raw = d.get(key)
        if raw is None or raw == "":
            return
        try:
            if isinstance(raw, dict):
                result[key] = raw
            elif isinstance(raw, str):
                result[key] = json.loads(raw)
        except Exception:
            logger.warning("firestore_get_cached: %s の解釈に失敗（スキップ） article_id=%s", key, article_id)

    for _k in ("quick_understand", "vote_data", "paper_graph", "paper_quiz", "deep_insights"):
        _merge_optional_dict_field(_k)
    return result


def firestore_delete_cache(article_id: str) -> bool:
    ref = _explanations_collection().document(article_id)
    meta_ref = _meta_doc()
    if ref.get().exists:
        ref.delete()
        _articles_collection().document(article_id).set({"has_explanation": False}, merge=True)
        try:
            meta = meta_ref.get()
            ids = list(meta.to_dict().get("ids", [])) if meta.exists else []
            if article_id in ids:
                ids = [x for x in ids if x != article_id]
                meta_ref.set({"ids": ids, "updated_at": _server_timestamp()})
        except Exception:
            pass
        return True
    return False


# 人格数（ai_service.PERSONAS の長さと一致させる）
_PERSONAS_COUNT = 14


def firestore_save_cache(
    article_id: str,
    blocks: list,
    personas: list,
    *,
    display_persona_ids: list | None = None,
    quick_understand: dict | None = None,
    vote_data: dict | None = None,
    paper_graph: dict | None = None,
    paper_quiz: dict | None = None,
    deep_insights: dict | None = None,
):
    if display_persona_ids is not None and len(display_persona_ids) == 3 and len(personas) == 3:
        personas_arr = list(personas)
        doc_data = {
            "inline_blocks": json.dumps(blocks, ensure_ascii=False),
            "personas": personas_arr,
            "display_persona_ids": list(display_persona_ids),
            "created_at": _server_timestamp(),
        }
    else:
        while len(personas) < _PERSONAS_COUNT:
            personas.append("")
        personas_arr = personas[:_PERSONAS_COUNT]
        doc_data = {
            "inline_blocks": json.dumps(blocks, ensure_ascii=False),
            "personas": personas_arr,
            "created_at": _server_timestamp(),
        }
    if quick_understand:
        doc_data["quick_understand"] = quick_understand
    if vote_data:
        doc_data["vote_data"] = vote_data
    if paper_graph:
        doc_data["paper_graph"] = paper_graph
    if paper_quiz:
        doc_data["paper_quiz"] = paper_quiz
    if deep_insights:
        doc_data["deep_insights"] = deep_insights
    _explanations_collection().document(article_id).set(doc_data)
    _articles_collection().document(article_id).set({"has_explanation": True}, merge=True)
    # メタの cached_article_ids を更新（1読1書）
    try:
        meta_ref = _meta_doc()
        meta = meta_ref.get()
        ids = list(meta.to_dict().get("ids", [])) if meta.exists else []
        if article_id not in ids:
            ids.append(article_id)
            meta_ref.set({"ids": ids, "updated_at": _server_timestamp()})
    except Exception:
        pass


def firestore_sync_meta_from_explanations() -> int:
    """
    explanations コレクションの doc id 一覧で _meta/cache の ids を上書きする。
    「記事は8件あるが表示は3件」のようなズレがあるときに実行すると解消する。
    戻り値: 同期した id の個数
    """
    ids = []
    for doc in _explanations_collection().limit(2000).stream():
        ids.append(doc.id)
    try:
        _meta_doc().set({"ids": ids, "updated_at": _server_timestamp()})
        logger.info("firestore_sync_meta_from_explanations: %d 件で _meta/cache を更新しました", len(ids))
    except Exception as e:
        logger.warning("firestore_sync_meta_from_explanations 失敗: %s", e)
        return 0
    return len(ids)


def use_firestore() -> bool:
    """Firestore を使用するか（認証情報があり、かつ firebase_admin がインストールされている場合のみ）"""
    if _load_credential_dict() is None:
        return False
    try:
        import firebase_admin  # noqa: F401
        return True
    except ModuleNotFoundError:
        # ローカルで firebase-admin 未インストール時は SQLite にフォールバック
        return False
