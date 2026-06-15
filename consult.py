#!/usr/bin/env python
"""偉人への相談 CLI

使い方:
  # LINE からの相談（対話モード）
  python consult.py

  # X のハッシュタグから自動収集して選ぶ
  python consult.py --from-x
  python consult.py --from-x --tag 知リポ相談
"""
import argparse
import sys
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


def main():
    parser = argparse.ArgumentParser(description="偉人への相談 CLI")
    parser.add_argument("--from-x", action="store_true", help="X ハッシュタグモードで起動")
    parser.add_argument("--tag", type=str, default="知リポ相談", help="X ハッシュタグ（# なし）")
    args = parser.parse_args()

    if args.from_x:
        _run_x_mode(args.tag)
    else:
        _run_line_mode()


if __name__ == "__main__":
    main()
