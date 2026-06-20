"""タイトルが単語の途中で42文字打ち切りになっている既存記事を再生成する一回限りのバッチ。

article_processor.py の _shorten() 単語境界バグ（修正済み）が原因で生まれた
末尾「…」の不完全なタイトルを、_rewrite_news_title() で再生成して直す。

使い方: プロジェクトルートから `python scripts/retitle_truncated_articles.py`
"""
import sys
import time
import psycopg2
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.services.article_processor import _rewrite_news_title
from app.services.translate_service import text_mainly_japanese


def find_truncated_articles() -> list[dict]:
    conn = psycopg2.connect(settings.DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, summary, category FROM articles "
                "WHERE title LIKE %s OR title LIKE %s",
                ("%…%", "%...%"),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [{"id": r[0], "title": r[1], "summary": r[2], "category": r[3]} for r in rows]


def update_title(article_id: str, new_title: str) -> None:
    conn = psycopg2.connect(settings.DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE articles SET title = %s WHERE id = %s", (new_title, article_id))
        conn.commit()
    finally:
        conn.close()


def main():
    targets = find_truncated_articles()
    print(f"対象: {len(targets)}件")
    ok, skipped = 0, []
    for i, a in enumerate(targets):
        new_title = _rewrite_news_title(a["title"] or "", a["summary"] or "", a["category"] or "")
        new_title = (new_title or "").strip()
        # 再生成後も末尾が省略記号、または元と同じ、または日本語化できていない場合はスキップ
        if not new_title or new_title.endswith("…") or new_title.endswith("...") or not text_mainly_japanese(new_title):
            skipped.append(a["id"])
            print(f"[SKIP] {a['id']}: {(a['title'] or '')[:50]}")
        else:
            update_title(a["id"], new_title)
            ok += 1
            print(f"[OK] {a['id']}: {(a['title'] or '')[:35]} -> {new_title[:35]}")
        if i < len(targets) - 1:
            time.sleep(1.5)

    print(f"\n完了: {ok}/{len(targets)} 件成功, スキップ {len(skipped)} 件")
    if skipped:
        print("スキップID:", skipped)


if __name__ == "__main__":
    main()
