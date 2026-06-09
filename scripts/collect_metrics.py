"""統計メトリクス 手動収集スクリプト

使い方:
  cd d:/app/newsite
  python scripts/collect_metrics.py                     # 全ソース収集
  python scripts/collect_metrics.py --source estat      # 特定ソースのみ
  python scripts/collect_metrics.py --category 人口     # カテゴリ指定

オプション:
  --source     特定コレクターのみ実行（estat/dashboard/mof/mhlw/immigration/mext/npa/mlit/cao/etl）
  --category   指定カテゴリに関連するコレクターのみ実行
  --dry-run    取得のみ行い DB 保存しない（確認用）
  --list       利用可能なコレクター一覧を表示
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.metrics_service import (
    ALL_COLLECTORS,
    collect_all_metrics,
    collect_metrics_by_category,
    EstatCollector,
    DashboardCollector,
    MoFCollector,
    MHLWCollector,
    ImmigrationCollector,
    MEXTCollector,
    NPACollector,
    MLITCollector,
    CAOCollector,
    ETLCollector,
)

SOURCE_MAP = {
    "estat":       EstatCollector(),
    "dashboard":   DashboardCollector(),
    "mof":         MoFCollector(),
    "mhlw":        MHLWCollector(),
    "immigration": ImmigrationCollector(),
    "mext":        MEXTCollector(),
    "npa":         NPACollector(),
    "mlit":        MLITCollector(),
    "cao":         CAOCollector(),
    "etl":         ETLCollector(),
}


def main():
    parser = argparse.ArgumentParser(description="統計メトリクスを手動収集")
    parser.add_argument("--source", help="特定コレクターのみ実行")
    parser.add_argument("--category", help="カテゴリ指定（カンマ区切りで複数可）")
    parser.add_argument("--dry-run", action="store_true", help="取得のみ（DB保存なし）")
    parser.add_argument("--list", action="store_true", help="コレクター一覧を表示")
    args = parser.parse_args()

    if args.list:
        print("利用可能なコレクター:")
        for key, c in SOURCE_MAP.items():
            print(f"  {key}: {c.source_name}")
        return

    if args.dry_run:
        source_key = args.source or list(SOURCE_MAP.keys())[0]
        collector = SOURCE_MAP.get(source_key)
        if not collector:
            print(f"エラー: 不明なソース '{source_key}'")
            sys.exit(1)
        print(f"[dry-run] {collector.source_name} からデータ取得...")
        rows = collector.fetch()
        print(f"取得件数: {len(rows)}")
        for r in rows[:10]:
            print(f"  [{r.category}] {r.name}: {r.value} {r.unit} ({r.year}年)")
        if len(rows) > 10:
            print(f"  ... 他 {len(rows)-10} 件")
        return

    if args.source:
        collector = SOURCE_MAP.get(args.source)
        if not collector:
            print(f"エラー: 不明なソース '{args.source}'")
            print("利用可能:", list(SOURCE_MAP.keys()))
            sys.exit(1)
        print(f"=== {collector.source_name} 収集開始 ===")
        count = collector.collect_and_save()
        print(f"完了: {count} 件保存")
    elif args.category:
        cats = [c.strip() for c in args.category.split(",")]
        print(f"=== カテゴリ {cats} の収集開始 ===")
        count = collect_metrics_by_category(cats)
        print(f"完了: {count} 件保存")
    else:
        print("=== 全ソース収集開始 ===")
        total = collect_all_metrics()
        print(f"完了: 合計 {total} 件保存")

    print(f"確認: http://localhost:8001/metrics?limit=20")


if __name__ == "__main__":
    main()
