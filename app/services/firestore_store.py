"""Firestore ストア - 記事・解説を Firestore に永続化（Render 等での永続化対応）。
無料枠（読 5万/日・書 2万/日）を考慮し、cached_article_ids はメタ1ドキュメントで管理・load_all は limit 付き。"""
import json
import logging
import threading
import time
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


def firestore_meta_cache_fingerprint() -> tuple:
    """_meta/cache を 1 回だけ読み、解説付き ID 一覧の変化検知用の軽い署名（件数・末尾 id・更新時刻）。"""
    try:
        meta = _meta_doc().get()
    except Exception:
        return (-1, "", "")
    if not meta.exists:
        return (0, "", "")
    d = meta.to_dict() or {}
    ids = d.get("ids") or []
    tail = str(ids[-1]) if ids else ""
    ut_raw = d.get("updated_at")
    try:
        ut = ut_raw.isoformat() if hasattr(ut_raw, "isoformat") else str(ut_raw)
    except Exception:
        ut = str(ut_raw)
    return (len(ids), tail, ut)


# Firestore: articles 全件メモリスナップショット（保存・削除で無効化。閲覧は原則これ経由で読み取り0）
_articles_snapshot_lock = threading.Lock()
_articles_snapshot: list | None = None
_articles_snapshot_by_id: dict[str, object] | None = None  # id -> NewsItem
# articles 全件ストリームの二重実行防止（起動ウォームと初回アクセスが同時だと全件を 2 倍読みになり極端に重い）
_load_all_build_lock = threading.Lock()

# 論文トップ用一覧のメモリキャッシュ（同一プロセス内の Firestore 読取削減）
_papers_site_list_lock = threading.Lock()
_papers_site_list_at: float = 0.0
_papers_site_list_cap: int = 0
_papers_site_list_items: list | None = None

# _meta/cache の ids を batch get するときの参照数（Firestore の getAll 都合で控えめ）
_PAPERS_META_BATCH = 100


def firestore_merge_article_into_snapshot(item) -> None:
    """既存の articles メモリスナップショットに 1 件を上書き／追加する（Firestore の再ストリームなし）。"""
    global _articles_snapshot, _articles_snapshot_by_id
    from dataclasses import replace

    from .rss_service import NewsItem

    if not isinstance(item, NewsItem):
        return
    with _articles_snapshot_lock:
        if _articles_snapshot_by_id is None or _articles_snapshot is None:
            return
        eff_added = item.added_at or item.published
        merged = replace(item, added_at=eff_added)
        _articles_snapshot_by_id[item.id] = merged
        _articles_snapshot = sorted(
            _articles_snapshot_by_id.values(),
            key=lambda x: x.added_at or x.published or datetime.min,
            reverse=True,
        )


def firestore_soft_refresh_after_article_write(merge_item=None) -> None:
    """解説メモリ・論文一覧 TTL を捨てるが、articles 全件スナップショットとニュース一覧キャッシュは保持（新着時の読み取り抑制）。"""
    if merge_item is not None:
        firestore_merge_article_into_snapshot(merge_item)
    try:
        from .explanation_cache import clear_explanation_memory_cache

        clear_explanation_memory_cache()
    except Exception:
        pass
    firestore_invalidate_papers_site_list_cache()


def firestore_invalidate_articles_snapshot() -> None:
    """articles 全件スナップショット・ニュース一覧キャッシュ・論文TTL・解説メモリを破棄（記事・解説の保存・削除後）。"""
    global _articles_snapshot, _articles_snapshot_by_id
    with _articles_snapshot_lock:
        _articles_snapshot = None
        _articles_snapshot_by_id = None
    try:
        from .explanation_cache import clear_explanation_memory_cache

        clear_explanation_memory_cache()
    except Exception:
        pass
    firestore_invalidate_papers_site_list_cache()
    try:
        from .news_aggregator import NewsAggregator

        NewsAggregator._news_cache = []
    except Exception:
        pass


def firestore_invalidate_papers_site_list_cache() -> None:
    """論文トップ用のメモリキャッシュを破棄。解説・記事の保存・削除・メタ同期後に呼ぶ。"""
    global _papers_site_list_at, _papers_site_list_cap, _papers_site_list_items
    with _papers_site_list_lock:
        _papers_site_list_items = None
        _papers_site_list_at = 0.0
        _papers_site_list_cap = 0
    try:
        from .news_aggregator import NewsAggregator

        NewsAggregator._invalidate_papers_cache()
    except Exception:
        pass

# --- articles ---
def firestore_load_by_id(article_id: str):
    from .rss_service import NewsItem, sanitize_display_text

    with _articles_snapshot_lock:
        snap = _articles_snapshot_by_id
    if snap is not None:
        hit = snap.get(article_id)
        if hit is not None:
            return hit  # type: ignore
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


def _firestore_stream_all_articles_into_memory():
    """Firestore の articles を全件読み（order_by なしで added_at 欠落ドキュメントも漏れない）、メモリで新しい順に並べる。"""
    from .rss_service import NewsItem, sanitize_display_text

    items: list = []
    by_id: dict[str, NewsItem] = {}
    # order_by("added_at") だと added_at 未設定の記事がクエリから除外され一覧に出ないことがあるため、全件 stream してからソートする。
    for doc in _articles_collection().stream():
        d = doc.to_dict() or {}
        try:
            pub = datetime.fromisoformat(d.get("published", "")) if d.get("published") else datetime.now()
        except Exception:
            pub = datetime.now()
        added_at_raw = d.get("added_at")
        added_at = added_at_raw.replace(tzinfo=None) if hasattr(added_at_raw, "replace") else None
        item = NewsItem(
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
        items.append(item)
        by_id[doc.id] = item
    items.sort(
        key=lambda x: x.added_at or x.published or datetime.min,
        reverse=True,
    )
    return items, by_id


def firestore_load_all():
    """保存済み記事を新しい順で全件返す。初回のみ Firestore を全走査し、以降はメモリスナップショット（保存時まで再読なし）。"""
    global _articles_snapshot, _articles_snapshot_by_id
    with _articles_snapshot_lock:
        if _articles_snapshot is not None:
            return list(_articles_snapshot)
    with _load_all_build_lock:
        with _articles_snapshot_lock:
            if _articles_snapshot is not None:
                return list(_articles_snapshot)
        built, by_id = _firestore_stream_all_articles_into_memory()
        with _articles_snapshot_lock:
            if _articles_snapshot is None:
                # 0件でスナップショットを載せると「一瞬0件」や不整合で永久に空のまま固定されるため、件数があるときだけキャッシュする。
                if built:
                    _articles_snapshot = built
                    _articles_snapshot_by_id = by_id
                return list(built)
            return list(_articles_snapshot)


def firestore_warm_articles_snapshot() -> int:
    """起動時など: 全 articles を一度読みメモリに載せる。戻り値は載せた件数。"""
    items = firestore_load_all()
    return len(items)


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


def _firestore_get_meta_ids_ordered() -> list[str]:
    """_meta/cache の ids を1読で取得（順序は保存時の配列順を維持）。"""
    meta = _meta_doc().get()
    if not meta.exists:
        return []
    raw = (meta.to_dict() or {}).get("ids")
    if not raw:
        return []
    return [str(x) for x in raw if x]


def _firestore_ensure_meta_ids_ordered() -> list[str]:
    """メタに ids が無いが explanations にある場合は再構築してから返す。"""
    ids = _firestore_get_meta_ids_ordered()
    if ids:
        return ids
    try:
        if not any(_explanations_collection().limit(1).stream()):
            return []
    except Exception:
        return []
    logger.warning(
        "_meta/cache の ids が空だが explanations にデータがあります。メタを explanations から再構築します。"
    )
    _rebuild_cached_article_ids_meta()
    return _firestore_get_meta_ids_ordered()


def _firestore_load_papers_from_meta_batch_get(meta_ids: list[str], cap: int) -> list:
    """メタの article_id を batch get し、研究・論文だけ最大 cap 件まで集める（added_at 全件走査しない）。"""
    if not meta_ids or cap <= 0:
        return []
    cap = max(1, min(int(cap), 50000))
    client = _get_client()
    col = _articles_collection()
    out: list = []
    # 新しい解説は ids の末尾に付くことが多いので後ろから batch 取得し、早めに cap に到達しやすくする
    ordered = list(reversed(meta_ids))
    for i in range(0, len(ordered), _PAPERS_META_BATCH):
        if len(out) >= cap:
            break
        chunk = ordered[i : i + _PAPERS_META_BATCH]
        refs = [col.document(aid) for aid in chunk if aid]
        if not refs:
            continue
        try:
            snaps = client.get_all(refs)
        except Exception as e:
            logger.warning("_firestore_load_papers_from_meta_batch_get: get_all 失敗 (%s)", e)
            snaps = []
            for r in refs:
                try:
                    snaps.append(r.get())
                except Exception:
                    pass
        for doc in snaps:
            if not getattr(doc, "exists", False):
                continue
            d = doc.to_dict() or {}
            if (d.get("category") or "").strip() != "研究・論文":
                continue
            out.append(_firestore_article_doc_to_item(doc.id, d))
            if len(out) >= cap:
                break
    return out


def _firestore_scan_papers_category_only(cap: int) -> list:
    """解説IDメタが空などの救済: 研究・論文カテゴリのみ（has_explanation や解説メタに依存しない）。"""
    cap = max(1, min(int(cap), 50000))
    scan = min(max(cap * 6, 5000), 75000)
    out: list = []
    for doc in _articles_collection().order_by("added_at", direction="DESCENDING").limit(scan).stream():
        d = doc.to_dict() or {}
        if (d.get("category") or "").strip() != "研究・論文":
            continue
        out.append(_firestore_article_doc_to_item(doc.id, d))
        if len(out) >= cap:
            break
    return out


def _firestore_scan_papers_with_processed(processed_ids: set[str], cap: int) -> list:
    """articles を added_at 降順にページ走査し、解説付きIDかつ研究・論文だけ集める（has_explanation フラグは使わない）。"""
    if not processed_ids:
        return []
    cap = max(1, min(int(cap), 50000))
    out: list = []
    page_size = 4000
    max_batches = 50
    last_snap = None
    for _ in range(max_batches):
        q = _articles_collection().order_by("added_at", direction="DESCENDING").limit(page_size)
        if last_snap is not None:
            q = q.start_after(last_snap)
        docs = list(q.stream())
        if not docs:
            break
        for doc in docs:
            if doc.id not in processed_ids:
                continue
            d = doc.to_dict() or {}
            if (d.get("category") or "").strip() != "研究・論文":
                continue
            out.append(_firestore_article_doc_to_item(doc.id, d))
            if len(out) >= cap:
                break
        last_snap = docs[-1]
        if len(out) >= cap or len(docs) < page_size:
            break
    return out


def firestore_load_all_papers_for_site_list(limit: int = 20000) -> list:
    """論文トップ SSR 用。_meta/cache の id 順で articles を batch get し、研究・論文を列挙。
    メタが空・batch 失敗時は従来の走査にフォールバック。結果は短時間 TTL でメモリキャッシュする。"""
    cap = max(1, min(int(limit), 50000))
    try:
        ttl = float(getattr(settings, "PAPERS_SITE_LIST_CACHE_TTL_SEC", 120))
    except Exception:
        ttl = 120.0
    now = time.monotonic()
    global _papers_site_list_at, _papers_site_list_cap, _papers_site_list_items
    with _papers_site_list_lock:
        if (
            _papers_site_list_items is not None
            and _papers_site_list_cap == cap
            and (now - _papers_site_list_at) < ttl
        ):
            return list(_papers_site_list_items)

    items: list = []
    try:
        meta_ids = _firestore_ensure_meta_ids_ordered()
    except Exception as e:
        logger.warning("firestore_load_all_papers_for_site_list: メタ取得に失敗 (%s)", e)
        meta_ids = []

    if not meta_ids:
        logger.warning(
            "firestore_load_all_papers_for_site_list: 解説付きIDが0件—カテゴリのみの論文一覧にフォールバックします"
        )
        items = _firestore_scan_papers_category_only(cap)
    else:
        try:
            items = _firestore_load_papers_from_meta_batch_get(meta_ids, cap)
        except Exception as e:
            logger.warning("firestore_load_all_papers_for_site_list: batch get 失敗 (%s)—スキャンにフォールバック", e)
            items = []
        if not items:
            try:
                items = _firestore_scan_papers_with_processed(set(meta_ids), cap)
            except Exception as e:
                logger.warning("firestore_load_all_papers_for_site_list: 積集合スキャン失敗 (%s)", e)
                items = []
        if not items:
            logger.warning(
                "firestore_load_all_papers_for_site_list: メタ経由で論文0件—カテゴリのみにフォールバックします"
            )
            items = _firestore_scan_papers_category_only(cap)

    out = sorted(
        items,
        key=lambda x: x.added_at or x.published or datetime.min,
        reverse=True,
    )[:cap]
    with _papers_site_list_lock:
        _papers_site_list_items = out
        _papers_site_list_at = time.monotonic()
        _papers_site_list_cap = cap
    return list(out)


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
            firestore_merge_article_into_snapshot(item)
        except Exception:
            pass
    if count:
        firestore_soft_refresh_after_article_write()
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
        firestore_soft_refresh_after_article_write(merge_item=item)
        return True
    except Exception:
        return False


def firestore_delete_article(article_id: str) -> bool:
    ref = _articles_collection().document(article_id)
    if ref.get().exists:
        ref.delete()
        firestore_invalidate_articles_snapshot()
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


def _meta_append_id(article_id: str, retries: int = 2) -> bool:
    """_meta/cache の ids に article_id を追記する。失敗時は retries 回リトライ。
    戻り値: 成功した（または既に含まれていた）場合 True。"""
    import time as _time
    for attempt in range(retries + 1):
        try:
            meta_ref = _meta_doc()
            meta = meta_ref.get()
            ids = list(meta.to_dict().get("ids", [])) if meta.exists else []
            if article_id not in ids:
                ids.append(article_id)
                meta_ref.set({"ids": ids, "updated_at": _server_timestamp()})
            return True
        except Exception as e:
            if attempt < retries:
                _time.sleep(0.4 * (attempt + 1))
            else:
                logger.warning("_meta/cache への %s 追記が %d 回すべて失敗: %s", article_id, retries + 1, e)
    return False


def firestore_check_explanation_and_repair_meta(article_id: str) -> bool:
    """explanations に解説が存在すれば _meta/cache を修復する。
    _meta/cache が更新されずに記事が非表示になった場合の自己修復用。
    Firestore 読み取り: 1（explanations ドキュメント）＋修復時に 1読1書。
    戻り値: 解説が存在した（修復を試みた）場合 True。"""
    try:
        if _explanations_collection().document(article_id).get().exists:
            _meta_append_id(article_id)
            return True
    except Exception as e:
        logger.warning("firestore_check_explanation_and_repair_meta 失敗 (%s): %s", article_id, e)
    return False


def _rebuild_cached_article_ids_meta() -> set:
    """explanations を全件スキャンして _meta/cache の ids を書き直す（初回・不整合修復用）。
    旧実装の limit(2000) 打ち切りでは、2000件以降の解説付き記事が一覧から消える。"""
    ids: list[str] = []
    try:
        for doc in _explanations_collection().stream():
            ids.append(doc.id)
    except Exception as e:
        logger.warning("_rebuild_cached_article_ids_meta: explanations 全件走査に失敗 (%s)", e)
        return set()
    try:
        _meta_doc().set({"ids": ids, "updated_at": _server_timestamp()})
    except Exception as e:
        logger.warning("_rebuild_cached_article_ids_meta: メタ書き込み失敗（ids=%d 件）: %s", len(ids), e)
        return set()
    firestore_invalidate_articles_snapshot()
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
        firestore_invalidate_articles_snapshot()
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
    # _meta/cache に ID を追記（リトライあり。失敗は警告ログに残し upsert 側で自己修復）
    _meta_append_id(article_id)
    firestore_soft_refresh_after_article_write()
    try:
        from .news_aggregator import NewsAggregator

        NewsAggregator.upsert_article_in_news_cache(article_id)
    except Exception:
        pass


def firestore_sync_meta_from_explanations() -> int:
    """
    explanations コレクションの doc id 一覧で _meta/cache の ids を上書きする。
    「記事は8件あるが表示は3件」のようなズレがあるときに実行すると解消する。
    戻り値: 同期した id の個数
    """
    try:
        n = len(_rebuild_cached_article_ids_meta())
        logger.info("firestore_sync_meta_from_explanations: %d 件で _meta/cache を更新しました", n)
        return n
    except Exception as e:
        logger.warning("firestore_sync_meta_from_explanations 失敗: %s", e)
        return 0


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
