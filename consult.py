#!/usr/bin/env python
"""偉人への相談 CLI

使い方:
  # LINE からの相談（対話モード）
  python consult.py

  # X のハッシュタグから自動収集して選ぶ
  python consult.py --from-x
  python consult.py --from-x --tag 知リポ相談

  # X の急上昇ポストを取得 → 要約 → 偉人がコメント → 140字に変換
  python consult.py --from-trends
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _show_personas():
    from app.services.ai_service import PERSONAS
    print("\n--- 偉人一覧 ---")
    for p in PERSONAS:
        print(f"  {p['id']:2d} : {p.get('emoji', '')} {p['name']}")
    print("----------------")


def _pick_persona() -> tuple[int, str, str]:
    from app.services.ai_service import PERSONAS
    _show_personas()
    while True:
        raw = input("答える偉人の番号: ").strip()
        try:
            pid = int(raw)
            if 0 <= pid < len(PERSONAS):
                p = PERSONAS[pid]
                return pid, p["name"], p.get("emoji", "")
        except ValueError:
            pass
        print("正しい番号を入力してください")


def _run_line_mode():
    print("\n=== LINE 相談モード ===")
    print("相談内容を貼り付けてください（空行で確定）:\n")
    lines = []
    try:
        while True:
            line = input()
            if line == "" and lines:
                break
            lines.append(line)
    except EOFError:
        pass
    question = "\n".join(lines).strip()
    if not question:
        print("相談内容が空です。終了します。")
        return

    print(f"\n入力内容:\n{question}\n")
    pid, pname, pemoji = _pick_persona()

    print(f"\n{pemoji} {pname} の回答を生成中...")
    from app.services.consultation_service import generate_consultation_answer
    answer = generate_consultation_answer(pid, question)
    print(f"\n--- 生成された回答 ---\n{answer}\n---\n")

    confirm = input("この内容で公開しますか？ (y/N): ").strip().lower()
    if confirm != "y":
        print("キャンセルしました。")
        return

    from app.services.consultation_store import save_consultation
    cid = save_consultation(
        question=question,
        persona_id=pid,
        persona_name=pname,
        persona_emoji=pemoji,
        answer=answer,
        source="line",
    )
    print(f"\n公開しました！ (id={cid})")
    print("/consultation で確認できます。")


def _run_x_mode(tag: str):
    print(f"\n=== X ハッシュタグモード: #{tag} ===")
    print("投稿を取得中...")
    from app.services.consultation_service import fetch_x_posts_by_tag
    posts = fetch_x_posts_by_tag(tag, limit=20)

    if not posts:
        print("投稿が見つかりませんでした。タグ名を確認するか、後で再試行してください。")
        return

    print(f"\n{len(posts)} 件の投稿:\n")
    for i, post in enumerate(posts, 1):
        preview = post["text"][:80].replace("\n", " ")
        print(f"  [{i:2d}] @{post['user']}: {preview}")

    print()
    while True:
        raw = input(f"番号を選択 (1-{len(posts)}, 0でキャンセル): ").strip()
        try:
            choice = int(raw)
            if choice == 0:
                print("キャンセルしました。")
                return
            if 1 <= choice <= len(posts):
                selected = posts[choice - 1]
                break
        except ValueError:
            pass
        print("正しい番号を入力してください")

    question = selected["text"]
    source_user = selected["user"]
    print(f"\n選択した相談:\n@{source_user}: {question}\n")

    pid, pname, pemoji = _pick_persona()

    print(f"\n{pemoji} {pname} の回答を生成中...")
    from app.services.consultation_service import generate_consultation_answer
    answer = generate_consultation_answer(pid, question)
    print(f"\n--- 生成された回答 ---\n{answer}\n---\n")

    confirm = input("この内容で公開しますか？ (y/N): ").strip().lower()
    if confirm != "y":
        print("キャンセルしました。")
        return

    from app.services.consultation_store import save_consultation
    cid = save_consultation(
        question=question,
        persona_id=pid,
        persona_name=pname,
        persona_emoji=pemoji,
        answer=answer,
        source="x",
        source_user=source_user,
    )
    print(f"\n公開しました！ (id={cid})")
    print("/consultation で確認できます。")


def _run_trends_mode():
    print("\n=== X 急上昇ポストモード ===")
    print("急上昇ワード・投稿を取得中...")
    from app.services.twitter_trends_service import fetch_trending_posts
    posts = fetch_trending_posts(limit=15)

    if not posts:
        print("投稿が見つかりませんでした。後で再試行してください。")
        return

    print(f"\n{len(posts)} 件の投稿:\n")
    for i, post in enumerate(posts, 1):
        preview = post.text[:80].replace("\n", " ")
        print(f"  [{i:2d}] #{post.keyword} @{post.user}: {preview}")
        if post.url:
            print(f"        {post.url}")

    print()
    while True:
        raw = input(f"番号を選択 (1-{len(posts)}, 0でキャンセル): ").strip()
        try:
            choice = int(raw)
            if choice == 0:
                print("キャンセルしました。")
                return
            if 1 <= choice <= len(posts):
                selected = posts[choice - 1]
                break
        except ValueError:
            pass
        print("正しい番号を入力してください")

    print(f"\n選択した投稿:\n#{selected.keyword} @{selected.user}: {selected.text}")
    if selected.url:
        print(f"元ポスト: {selected.url}")
    print()

    print("要約中...")
    from app.services.consultation_service import summarize_post_text
    summary = summarize_post_text(selected.text)
    print(f"\n--- 要約 ---\n{summary}\n---\n")

    pid, pname, pemoji = _pick_persona()

    print(f"\n{pemoji} {pname} のコメントを生成中...")
    from app.services.consultation_service import generate_consultation_answer, compress_to_140, format_trend_post
    comment = generate_consultation_answer(pid, summary)
    print(f"\n--- 生成されたコメント ---\n{comment}\n---\n")

    print("140文字以内に変換中...")
    compressed = compress_to_140(comment, limit=100)
    final_text = format_trend_post(pname, pemoji, compressed)
    print(f"\n--- X投稿用（{len(final_text)}字）---\n{final_text}")
    if selected.url:
        print(f"\n元ポスト（リプライ用）: {selected.url}")
    print("---\n")

    from app.services.notion_logger import create_xpost_page
    notion_ok = create_xpost_page(
        title=f"[急上昇] #{selected.keyword}",
        x_post=final_text,
        article_url=selected.url,
        persona_name=pname,
        category="Xトレンド",
        source=f"@{selected.user}",
        published=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    print("Notionに追加しました（元ポストへのリンク付き）。" if notion_ok else "Notion未設定のためスキップしました。")

    confirm = input("この内容で /consultation に公開しますか？ (y/N): ").strip().lower()
    if confirm != "y":
        print("公開はキャンセルしました（X投稿用テキストは上記からコピーして使えます）。")
        return

    from app.services.consultation_store import save_consultation
    cid = save_consultation(
        question=summary,
        persona_id=pid,
        persona_name=pname,
        persona_emoji=pemoji,
        answer=comment,
        source="trend",
        source_user=selected.user,
    )
    print(f"\n公開しました！ (id={cid})")
    print("/consultation で確認できます。")
    print(f"\nXに投稿する場合はこちらのテキストをコピーしてください:\n\n{final_text}")


def main():
    parser = argparse.ArgumentParser(description="偉人への相談 CLI")
    parser.add_argument("--from-x", action="store_true", help="X ハッシュタグモードで起動")
    parser.add_argument("--from-trends", action="store_true", help="X 急上昇ポストモードで起動")
    parser.add_argument("--tag", type=str, default="知リポ相談", help="X ハッシュタグ（# なし）")
    args = parser.parse_args()

    if args.from_trends:
        _run_trends_mode()
    elif args.from_x:
        _run_x_mode(args.tag)
    else:
        _run_line_mode()


if __name__ == "__main__":
    main()
