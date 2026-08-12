"""
記事・解説データを Turso へ移行する（冪等）。

入力元を切り替えられる:
  --source sqlite   ローカルの data/articles.db + data/explanations.db から
  --source neon     Neon Postgres（DATABASE_URL）から

同じ id は ON CONFLICT DO UPDATE で上書きするため、何度実行しても安全。
「まずローカルの古い DB で開始し、Neon 復旧後に --source neon で流し直して
最新データへ差し替える」という二段階移行を想定している。

使い方:
  set TURSO_DATABASE_URL=libsql://...
  set TURSO_AUTH_TOKEN=...
  python scripts/migrate_to_turso.py --source sqlite --dry-run
  python scripts/migrate_to_turso.py --source sqlite
"""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
PERSONAS_COUNT = 14


def _log(msg: str) -> None:
    print(msg, flush=True)


def set_data_dir(path) -> None:
    """入力元のローカル DB ディレクトリを差し替える（worktree から本体を指す用）。"""
    global DATA_DIR
    DATA_DIR = Path(path)


# --- 入力元: ローカル SQLite ---

def _read_sqlite_articles() -> list[dict]:
    path = DATA_DIR / "articles.db"
    if not path.exists():
        _log(f"  articles.db が見つかりません: {path}")
        return []
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, title, link, summary, published, source, category, image_url, added_at "
            "FROM articles"
        ).fetchall()
    except sqlite3.OperationalError:
        # 古い DB は added_at が無いことがある
        rows = conn.execute(
            "SELECT id, title, link, summary, published, source, category, image_url "
            "FROM articles"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _read_sqlite_explanations() -> list[dict]:
    path = DATA_DIR / "explanations.db"
    if not path.exists():
        _log(f"  explanations.db が見つかりません: {path}")
        return []
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute("PRAGMA table_info(explanation_cache)")}
    rows = conn.execute("SELECT * FROM explanation_cache").fetchall()
    conn.close()

    out = []
    for r in rows:
        d = dict(r)
        personas = None
        if "personas" in cols and d.get("personas"):
            try:
                personas = json.loads(d["personas"])
            except Exception:
                personas = None
        if not isinstance(personas, list):
            # 旧スキーマ: persona_0..persona_4 を集約する
            personas = [d.get(f"persona_{i}") or "" for i in range(5)]
        personas = (list(personas) + [""] * PERSONAS_COUNT)[:PERSONAS_COUNT]

        out.append({
            "article_id": d.get("article_id"),
            "inline_blocks": d.get("inline_blocks") or "",
            "personas": json.dumps(personas, ensure_ascii=False),
            "display_persona_ids": d.get("display_persona_ids"),
            # 旧スキーマにはこれらの列が無い。Neon 復旧後の再実行で埋まる。
            "quick_understand": d.get("quick_understand"),
            "vote_data": d.get("vote_data"),
            "paper_graph": d.get("paper_graph"),
            "paper_quiz": d.get("paper_quiz"),
            "deep_insights": d.get("deep_insights"),
            "editorial_take": d.get("editorial_take") or "",
            "created_at": d.get("created_at"),
        })
    return out


# --- 入力元: Neon ---

def _read_neon_articles() -> list[dict]:
    from app.services.neon_store import _conn

    with _conn("migrate_read_articles") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, link, summary, published, source, category, image_url, added_at "
                "FROM articles"
            )
            rows = cur.fetchall()
    cols = ["id", "title", "link", "summary", "published", "source",
            "category", "image_url", "added_at"]
    return [dict(zip(cols, r)) for r in rows]


def _read_neon_explanations() -> list[dict]:
    from app.services.neon_store import _conn

    cols = ["article_id", "inline_blocks", "personas", "display_persona_ids",
            "quick_understand", "vote_data", "paper_graph", "paper_quiz",
            "deep_insights", "editorial_take", "created_at"]
    with _conn("migrate_read_explanations") as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {', '.join(cols)} FROM explanations")
            rows = cur.fetchall()
    return [dict(zip(cols, r)) for r in rows]


# --- 書き込み ---

def _write_articles(rows: list[dict], dry_run: bool) -> int:
    from app.services.turso_store import _commit, _dt_to_text, _execute

    n = 0
    for d in rows:
        if not d.get("id"):
            continue
        if dry_run:
            n += 1
            continue
        _execute(
            """
            INSERT INTO articles
                (id, title, link, summary, published, source, category, image_url, added_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                link = excluded.link,
                summary = excluded.summary,
                published = excluded.published,
                source = excluded.source,
                category = excluded.category,
                image_url = excluded.image_url,
                added_at = COALESCE(excluded.added_at, articles.added_at)
            """,
            (
                d["id"],
                d.get("title") or "",
                d.get("link") or "",
                d.get("summary") or "",
                _dt_to_text(d.get("published")),
                d.get("source") or "",
                d.get("category") or "総合",
                d.get("image_url"),
                _dt_to_text(d.get("added_at")),
            ),
        )
        n += 1
        if n % 100 == 0:
            _commit()
            _log(f"    articles {n} 件")
    if not dry_run:
        _commit()
    return n


def _write_explanations(rows: list[dict], dry_run: bool) -> int:
    from app.services.turso_store import _commit, _dt_to_text, _execute

    n = 0
    for d in rows:
        aid = d.get("article_id")
        if not aid or not d.get("inline_blocks"):
            continue
        if dry_run:
            n += 1
            continue
        _execute(
            """
            INSERT INTO explanations
                (article_id, inline_blocks, personas, display_persona_ids,
                 quick_understand, vote_data, paper_graph, paper_quiz,
                 deep_insights, editorial_take, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            ON CONFLICT(article_id) DO UPDATE SET
                inline_blocks = excluded.inline_blocks,
                personas = excluded.personas,
                display_persona_ids = excluded.display_persona_ids,
                quick_understand = COALESCE(excluded.quick_understand, explanations.quick_understand),
                vote_data = COALESCE(excluded.vote_data, explanations.vote_data),
                paper_graph = COALESCE(excluded.paper_graph, explanations.paper_graph),
                paper_quiz = COALESCE(excluded.paper_quiz, explanations.paper_quiz),
                deep_insights = COALESCE(excluded.deep_insights, explanations.deep_insights),
                editorial_take = excluded.editorial_take
            """,
            (
                aid,
                d.get("inline_blocks"),
                d.get("personas"),
                d.get("display_persona_ids"),
                d.get("quick_understand"),
                d.get("vote_data"),
                d.get("paper_graph"),
                d.get("paper_quiz"),
                d.get("deep_insights"),
                d.get("editorial_take") or "",
                _dt_to_text(d.get("created_at")),
            ),
        )
        n += 1
        if n % 100 == 0:
            _commit()
            _log(f"    explanations {n} 件")
    if not dry_run:
        _commit()
    return n


def _sync_has_explanation(dry_run: bool) -> int:
    """explanations に行がある記事へ has_explanation を立て直す。"""
    from app.services.turso_store import _commit, _execute

    if dry_run:
        return 0
    _execute(
        "UPDATE articles SET has_explanation = 1 "
        "WHERE id IN (SELECT article_id FROM explanations)"
    )
    _commit()
    rows, _, _ = _execute("SELECT COUNT(*) FROM articles WHERE has_explanation = 1")
    return rows[0][0] if rows else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["sqlite", "neon"], default="sqlite")
    ap.add_argument("--dry-run", action="store_true", help="読み取りと件数確認のみ")
    ap.add_argument("--skip-articles", action="store_true")
    ap.add_argument("--skip-explanations", action="store_true")
    ap.add_argument(
        "--data-dir",
        default=None,
        help="ローカル DB の場所（既定: リポジトリ直下の data/）",
    )
    args = ap.parse_args()

    if args.data_dir:
        set_data_dir(args.data_dir)
        _log(f"データ元ディレクトリ: {DATA_DIR}")

    if not os.getenv("TURSO_DATABASE_URL"):
        _log("ERROR: TURSO_DATABASE_URL が未設定です")
        return 1

    from app.services.turso_store import init_schema, use_turso

    if not use_turso():
        _log("ERROR: Turso に接続できません（turso_serverless 未インストール？）")
        return 1

    _log(f"移行元: {args.source} / dry-run: {args.dry_run}")

    if not args.dry_run:
        _log("スキーマを作成中...")
        init_schema()

    total_a = total_e = 0

    if not args.skip_articles:
        _log("記事を読み込み中...")
        arts = _read_sqlite_articles() if args.source == "sqlite" else _read_neon_articles()
        _log(f"  読み込み: {len(arts)} 件")
        total_a = _write_articles(arts, args.dry_run)
        _log(f"  書き込み: {total_a} 件")

    if not args.skip_explanations:
        _log("解説を読み込み中...")
        expls = _read_sqlite_explanations() if args.source == "sqlite" else _read_neon_explanations()
        _log(f"  読み込み: {len(expls)} 件")
        total_e = _write_explanations(expls, args.dry_run)
        _log(f"  書き込み: {total_e} 件")

    flagged = _sync_has_explanation(args.dry_run)

    _log("")
    _log(f"完了: articles={total_a} explanations={total_e} has_explanation={flagged}")
    if args.dry_run:
        _log("(dry-run のため書き込みはしていません)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
