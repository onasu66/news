"""curated_articles.json の記事を既存パイプラインで記事化するCLIスクリプト

使い方（ローカル処理）:
    python run_curated.py
    python run_curated.py --file path/to/other.json
    python run_curated.py --max 10

使い方（Renderへ転送して処理）:
    python run_curated.py --push https://chirippo.onrender.com --secret <ADMIN_SECRET>
    python run_curated.py --push https://... --secret xxx --max 15
"""
import argparse
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def push_to_render(url: str, secret: str, json_file: Path, max_articles: int) -> None:
    """curated_articles.json を Render サーバーに送信して記事化を依頼する。"""
    import json
    import urllib.request

    endpoint = url.rstrip("/") + f"/api/admin/seed-curated?max={max_articles}"
    data = json.loads(json_file.read_text(encoding="utf-8"))
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Admin-Secret": secret,
        },
    )
    logger.info("Render に %d 件を送信中: %s", len(data), endpoint)
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            print(f"\n[完了] Render 記事化: {result.get('added', '?')} 件追加 / 合計 {result.get('total', '?')} 件")
    except urllib.error.HTTPError as e:
        body_err = e.read().decode("utf-8", errors="replace")
        logger.error("HTTP %d: %s", e.code, body_err[:300])
        sys.exit(1)
    except Exception as e:
        logger.error("通信エラー: %s", e)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="curated_articles.json を記事化する")
    parser.add_argument("--file", default=None, help="JSONファイルのパス（省略時: curated_articles.json）")
    parser.add_argument("--max", type=int, default=30, help="最大記事化件数（デフォルト: 30）")
    parser.add_argument("--push", default=None, metavar="URL",
                        help="Render サーバーの URL（例: https://chirippo.onrender.com）。指定するとローカル処理ではなくサーバーに転送する")
    parser.add_argument("--secret", default=None, help="X-Admin-Secret ヘッダの値（--push 時に必要）")
    args = parser.parse_args()

    fp = Path(args.file) if args.file else Path(__file__).resolve().parent / "curated_articles.json"

    if args.push:
        if not fp.exists():
            logger.error("ファイルが見つかりません: %s", fp)
            sys.exit(1)
        if not args.secret:
            logger.error("--push を使う場合は --secret も指定してください")
            sys.exit(1)
        push_to_render(args.push, args.secret, fp, args.max)
        return

    # ローカル処理
    from app.services.article_seed_from_curated import process_curated_articles
    count = process_curated_articles(path=fp if args.file else None, max_per_run=args.max)
    print(f"\n[完了] 記事化: {count} 件")
    sys.exit(0 if count >= 0 else 1)


if __name__ == "__main__":
    main()
