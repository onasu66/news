"""Neon / ローカル SQLite から Turso へ記事・解説を移行する。

使い方:
  python scripts/migrate_to_turso.py

優先順:
  1) Neon が読めるなら Neon から
  2) ダメなら data/articles.db + data/explanations.db から
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    from app.services.turso_store import (
        use_turso,
        turso_init_schema,
        turso_conn,
        turso_save_article,
        turso_save_cache,
    )
    from app.services.rss_service import NewsItem
    from datetime import datetime

    if not use_turso():
        print("ERROR: TURSO_DATABASE_URL / TURSO_AUTH_TOKEN が未設定です")
        return 1

    print("Turso スキーマ初期化...")
    turso_init_schema()

    articles = []
    explanations = {}

    # --- Neon 試行（TURSO_* があっても DATABASE_URL へ直接接続）---
    # use_postgres_neon() / _conn は Turso 設定時に Neon を拒否するため、移行専用に直結する。
    neon_ok = False
    try:
        import os

        import psycopg2
        import psycopg2.extras

        neon_url = os.getenv("DATABASE_URL", "").strip()
        if not neon_url:
            try:
                from app.config import settings

                neon_url = (getattr(settings, "DATABASE_URL", "") or "").strip()
            except Exception:
                neon_url = ""

        if neon_url:
            print("Neon から読み込み中（直接接続）...")
            conn = psycopg2.connect(neon_url, connect_timeout=30)
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT id, title, link, summary, published, source, category, image_url, added_at "
                        "FROM articles"
                    )
                    articles = [dict(r) for r in cur.fetchall()]
                    cur.execute(
                        "SELECT article_id, inline_blocks, personas, display_persona_ids, "
                        "quick_understand, vote_data, paper_graph, paper_quiz, deep_insights, editorial_take "
                        "FROM explanations"
                    )
                    explanations = {r["article_id"]: dict(r) for r in cur.fetchall()}
            finally:
                conn.close()
            neon_ok = True
            print(f"Neon: articles={len(articles)} explanations={len(explanations)}")
        else:
            print("DATABASE_URL 未設定のため Neon スキップ")
    except Exception as e:
        print(f"Neon 読み込み不可（想定どおりの場合あり）: {e}")
        articles = []
        explanations = {}

    # --- ローカル SQLite フォールバック ---
    if not neon_ok:
        adb = ROOT / "data" / "articles.db"
        edb = ROOT / "data" / "explanations.db"
        if adb.exists():
            print(f"ローカル {adb} から読み込み...")
            conn = sqlite3.connect(str(adb))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, title, link, summary, published, source, category, image_url, added_at FROM articles"
            ).fetchall()
            articles = [dict(r) for r in rows]
            conn.close()
        if edb.exists():
            print(f"ローカル {edb} から読み込み...")
            conn = sqlite3.connect(str(edb))
            conn.row_factory = sqlite3.Row
            # Neon互換 explanations または旧 explanation_cache
            try:
                rows = conn.execute(
                    "SELECT article_id, inline_blocks, personas, display_persona_ids, "
                    "quick_understand, vote_data, paper_graph, paper_quiz, deep_insights, editorial_take "
                    "FROM explanations"
                ).fetchall()
                explanations = {r["article_id"]: dict(r) for r in rows}
            except Exception:
                rows = conn.execute("SELECT * FROM explanation_cache").fetchall()
                for r in rows:
                    d = dict(r)
                    personas = []
                    if d.get("personas"):
                        try:
                            personas = json.loads(d["personas"])
                        except Exception:
                            personas = []
                    if not personas:
                        personas = [d.get(f"persona_{i}") or "" for i in range(5)]
                    explanations[d["article_id"]] = {
                        "article_id": d["article_id"],
                        "inline_blocks": d.get("inline_blocks"),
                        "personas": json.dumps(personas, ensure_ascii=False),
                        "display_persona_ids": d.get("display_persona_ids"),
                        "quick_understand": None,
                        "vote_data": None,
                        "paper_graph": None,
                        "paper_quiz": None,
                        "deep_insights": None,
                        "editorial_take": "",
                    }
            conn.close()
        print(f"ローカル: articles={len(articles)} explanations={len(explanations)}")

    if not articles:
        print("移行する記事がありません。空の Turso で新規運用を開始できます。")
        with turso_conn("count") as conn:
            n = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        print(f"Turso articles 現在: {n}")
        return 0

    saved = 0
    for a in articles:
        try:
            pub = a.get("published")
            if hasattr(pub, "isoformat"):
                published = pub
            else:
                try:
                    published = datetime.fromisoformat(str(pub).replace("Z", "+00:00")).replace(tzinfo=None) if pub else datetime.now()
                except Exception:
                    published = datetime.now()
            item = NewsItem(
                id=a["id"],
                title=a.get("title") or "",
                link=a.get("link") or "",
                summary=a.get("summary") or "",
                published=published,
                source=a.get("source") or "",
                category=a.get("category") or "総合",
                image_url=a.get("image_url"),
            )
            if turso_save_article(item):
                saved += 1
        except Exception as e:
            print(f"article skip {a.get('id')}: {e}")
    print(f"articles 保存: {saved}/{len(articles)}")

    exp_saved = 0
    for aid, e in explanations.items():
        try:
            blocks = json.loads(e["inline_blocks"]) if isinstance(e.get("inline_blocks"), str) else e.get("inline_blocks")
            if not blocks:
                continue
            personas = json.loads(e["personas"]) if isinstance(e.get("personas"), str) and e.get("personas") else (e.get("personas") or [])
            ids = None
            if e.get("display_persona_ids"):
                ids = json.loads(e["display_persona_ids"]) if isinstance(e["display_persona_ids"], str) else e["display_persona_ids"]

            def _maybe_json(v):
                if not v:
                    return None
                if isinstance(v, (dict, list)):
                    return v
                try:
                    return json.loads(v)
                except Exception:
                    return None

            turso_save_cache(
                aid,
                blocks,
                personas if isinstance(personas, list) else [],
                display_persona_ids=ids if isinstance(ids, list) else None,
                quick_understand=_maybe_json(e.get("quick_understand")),
                vote_data=_maybe_json(e.get("vote_data")),
                paper_graph=_maybe_json(e.get("paper_graph")),
                paper_quiz=_maybe_json(e.get("paper_quiz")),
                deep_insights=_maybe_json(e.get("deep_insights")),
                editorial_take=e.get("editorial_take") or "",
            )
            exp_saved += 1
        except Exception as ex:
            print(f"explanation skip {aid}: {ex}")
    print(f"explanations 保存: {exp_saved}/{len(explanations)}")

    with turso_conn("verify") as conn:
        a_n = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        e_n = conn.execute("SELECT COUNT(*) FROM explanations").fetchone()[0]
        h_n = conn.execute("SELECT COUNT(*) FROM articles WHERE has_explanation = 1").fetchone()[0]
    print(f"Turso 検証: articles={a_n} explanations={e_n} has_explanation={h_n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
