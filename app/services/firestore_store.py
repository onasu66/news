"""Firestore ストア - 記事・解説を Firestore に永続化（Render 等での永続化対応）。
無料枠（読 5万/日・書 2万/日）を考慮し、cached_article_ids はメタ1ドキュメントで管理・load_all は limit 付き。"""
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

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


# 一覧取得の上限（無料枠 5万読/日 を考慮。PAGE_DISPLAY_LIMIT と揃える）
_LOAD_ALL_LIMIT = 2000

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
    return NewsItem(
        id=doc.id,
        title=d.get("title", ""),
        link=d.get("link", ""),
        summary=sanitize_display_text(d.get("summary") or ""),
        published=pub,
        source=d.get("source", ""),
        category=d.get("category", "総合"),
        image_url=d.get("image_url"),
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
        items.append(NewsItem(
            id=doc.id,
            title=d.get("title", ""),
            link=d.get("link", ""),
            summary=sanitize_display_text(d.get("summary") or ""),
            published=pub,
            source=d.get("source", ""),
            category=d.get("category", "総合"),
            image_url=d.get("image_url"),
        ))
    return items


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


def firestore_get_cached_article_ids() -> set:
    """AI処理済み記事ID。メタドキュメント1読で返す（explanations 全件ストリーム廃止・読み取り削減）"""
    meta = _meta_doc().get()
    if meta.exists:
        ids = meta.to_dict().get("ids") or []
        return set(ids)
    # 初回またはメタ未構築: explanations を上限付きで1回だけスキャンしメタを構築
    ids = []
    for doc in _explanations_collection().limit(2000).stream():
        ids.append(doc.id)
    try:
        _meta_doc().set({"ids": ids, "updated_at": _server_timestamp()})
    except Exception:
        pass
    return set(ids)


def firestore_get_cached(article_id: str) -> Optional[dict]:
    doc = _explanations_collection().document(article_id).get()
    if not doc.exists:
        return None
    d = doc.to_dict()
    blocks = json.loads(d.get("inline_blocks", "[]"))
    if _is_bad_fallback_cache(blocks):
        return None
    if "personas" in d and isinstance(d["personas"], list):
        personas = (d["personas"] + [""] * 5)[:5]
    else:
        personas = [d.get(f"persona_{i}", "") or "" for i in range(5)]
    result = {"blocks": blocks, "personas": personas}
    if d.get("quick_understand"):
        result["quick_understand"] = d["quick_understand"] if isinstance(d["quick_understand"], dict) else json.loads(d["quick_understand"])
    if d.get("vote_data"):
        result["vote_data"] = d["vote_data"] if isinstance(d["vote_data"], dict) else json.loads(d["vote_data"])
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


def firestore_save_cache(article_id: str, blocks: list, personas: list, *, quick_understand: dict | None = None, vote_data: dict | None = None):
    while len(personas) < 5:
        personas.append("")
    personas_arr = personas[:5]
    doc_data = {
        "inline_blocks": json.dumps(blocks, ensure_ascii=False),
        "personas": personas_arr,
        "created_at": _server_timestamp(),
    }
    if quick_understand:
        doc_data["quick_understand"] = quick_understand
    if vote_data:
        doc_data["vote_data"] = vote_data
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
