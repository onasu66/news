"""curated_articles.json の記事を既存パイプラインで記事化するCLIスクリプト

使い方:
    python run_curated.py
    python run_curated.py --file path/to/other.json
    python run_curated.py --max 10
"""
import argparse
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def main():
    parser = argparse.ArgumentParser(description="curated_articles.json を記事化する")
    parser.add_argument("--file", default=None, help="JSONファイルのパス（省略時: curated_articles.json）")
    parser.add_argument("--max", type=int, default=30, help="最大記事化件数（デフォルト: 30）")
    args = parser.parse_args()

    fp = Path(args.file) if args.file else None

    from app.services.article_seed_from_curated import process_curated_articles
    count = process_curated_articles(path=fp, max_per_run=args.max)
    print(f"\n✅ 記事化完了: {count} 件")
    sys.exit(0 if count >= 0 else 1)


if __name__ == "__main__":
    main()
