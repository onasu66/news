"""既存記事で英語タイトルのまま残っているものを再翻訳してNeonを更新する一回限りのバッチ。

article_processor.py の strict_ja 判定バグ（FOREIGN_SOURCES にない CNBC/Bloomberg/TechCrunch 等で
翻訳失敗が素通りしていた）は修正済みだが、修正前に保存済みの記事はこのスクリプトで直す。

使い方: プロジェクトルートから `python scripts/retranslate_english_titles.py`
"""
import sys
import time
import psycopg2
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.services.rss_service import NewsItem
from app.services.translate_service import translate_and_rewrite, text_mainly_japanese
from app.services.article_cache import save_article


def _ascii_ratio(text: str) -> float:
    letters = [c for c in (text or "") if c.isalpha()]
    if len(letters) < 3:
        return 0.0
    ascii_letters = [c for c in letters if ord(c) < 128]
    return len(ascii_letters) / len(letters)


def find_english_titled_articles() -> list[dict]:
    conn = psycopg2.connect(settings.DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, summary, link, published, source, category, image_url "
                "FROM articles WHERE category != %s ORDER BY added_at DESC",
                ("研究・論文",),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    out = []
    for aid, title, summary, link, published, source, category, image_url in rows:
        if _ascii_ratio(title or "") > 0.5:
            out.append({
                "id": aid, "title": title, "summary": summary, "link": link,
                "published": published, "source": source, "category": category,
                "image_url": image_url,
            })
    return out


def main():
    targets = find_english_titled_articles()
    print(f"対象: {len(targets)}件")
    ok, failed = 0, []
    for i, a in enumerate(targets):
        new_title, new_summary = translate_and_rewrite(a["title"] or "", a["summary"] or "")
        if not text_mainly_japanese(new_title):
            failed.append(a["id"])
            print(f"[FAIL] {a['id']}: {(a['title'] or '')[:50]}")
        else:
            item = NewsItem(
                id=a["id"],
                title=new_title,
                link=a["link"],
                summary=new_summary if text_mainly_japanese(new_summary) else (a["summary"] or ""),
                published=a["published"],
                source=a["source"],
                category=a["category"],
                image_url=a["image_url"],
            )
            if save_article(item):
                ok += 1
                print(f"[OK] {a['id']}: {(a['title'] or '')[:40]} -> {new_title[:40]}")
            else:
                failed.append(a["id"])
                print(f"[SAVE_FAIL] {a['id']}")
        if i < len(targets) - 1:
            time.sleep(2)  # 翻訳APIのレート制限対策

    print(f"\n完了: {ok}/{len(targets)} 件成功, 失敗 {len(failed)} 件")
    if failed:
        print("失敗ID:", failed)


if __name__ == "__main__":
    main()
