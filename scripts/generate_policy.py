"""政策提案 手動生成スクリプト

使い方:
  cd d:/app/newsite
  python scripts/generate_policy.py --topic shoushika

オプション:
  --topic   トピックキー（デフォルト: shoushika）
  --list    利用可能なトピック一覧を表示して終了
  --dry-run 生成のみ行い DB 保存しない（確認用）
"""
import argparse
import json
import sys
from pathlib import Path

# プロジェクトルートを sys.path に追加
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.policy_ai_service import TOPIC_CONFIGS, generate_policy_proposals, run_generate_and_save


def main():
    parser = argparse.ArgumentParser(description="政策提案AIを手動実行")
    parser.add_argument("--topic", default="shoushika", help="トピックキー（デフォルト: shoushika）")
    parser.add_argument("--list", action="store_true", help="利用可能なトピック一覧を表示")
    parser.add_argument("--dry-run", action="store_true", help="生成のみ（DB保存しない）")
    args = parser.parse_args()

    if args.list:
        print("利用可能なトピック:")
        for key, cfg in TOPIC_CONFIGS.items():
            print(f"  {key}: {cfg['title']}")
        return

    topic_key = args.topic
    if topic_key not in TOPIC_CONFIGS:
        print(f"エラー: 未知のトピック '{topic_key}'")
        print("利用可能:", list(TOPIC_CONFIGS.keys()))
        sys.exit(1)

    print(f"=== 政策提案生成: {TOPIC_CONFIGS[topic_key]['title']} ===")

    if args.dry_run:
        print("[dry-run] 生成のみ実行（DB保存なし）")
        proposals = generate_policy_proposals(topic_key)
        if proposals:
            print(f"\n生成された施策 ({len(proposals)} 件):")
            for p in proposals:
                print(f"\n--- 施策 {p['rank']}: {p['title']} ---")
                print(f"  要約: {p['summary'][:80]}...")
                print(f"  コスト: {p['cost_estimate']}")
                print(f"  効果: {p['effect_prediction']}")
        else:
            print("生成失敗")
            sys.exit(1)
    else:
        ok = run_generate_and_save(topic_key)
        if ok:
            print(f"\n完了: トピック '{topic_key}' の提案を DB に保存しました")
            print(f"確認: http://localhost:8001/policy")
        else:
            print("失敗: 生成または保存に問題が発生しました")
            sys.exit(1)


if __name__ == "__main__":
    main()
