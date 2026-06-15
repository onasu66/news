"""知リポAIのPerplexity引用セルフチェック。

使い方:
  python scripts/check_perplexity_citations.py

確認内容:
  - Perplexity APIを使って代表的なクエリで自サイトが引用されているか調べる
  - 結果をコンソール出力 + logs/perplexity_citations.jsonl に追記

環境変数:
  PERPLEXITY_API_KEY  Perplexity の API キー（https://www.perplexity.ai/settings/api）
  SITE_URL            サイトURL（デフォルト: https://chiripo.ai）
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = ROOT / "logs" / "perplexity_citations.jsonl"

QUERIES = [
    "AI論文をわかりやすく解説するサイト",
    "偉人AIがニュースを解説",
    "arXiv論文 日本語解説",
    "知リポAI",
    "論文要約 AI 偉人",
    "ブッダ アインシュタイン AI論文解説",
]

SITE_URL = os.getenv("SITE_URL", "https://chiripo.ai").rstrip("/")
SITE_DOMAINS = ["chiripo.ai", "知リポ", "chiripoai"]


def _check_citation(query: str, api_key: str) -> dict:
    import urllib.request
    payload = json.dumps({
        "model": "llama-3.1-sonar-small-128k-online",
        "messages": [{"role": "user", "content": query}],
        "return_citations": True,
    }).encode()
    req = urllib.request.Request(
        "https://api.perplexity.ai/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        return {"query": query, "error": str(e), "cited": False, "citations": []}

    answer = ""
    citations = []
    try:
        answer = data["choices"][0]["message"]["content"]
        citations = data.get("citations", [])
    except Exception:
        pass

    cited = any(
        any(d in (c if isinstance(c, str) else c.get("url", "")) for d in SITE_DOMAINS)
        for c in citations
    ) or any(d in answer for d in SITE_DOMAINS)

    return {
        "query": query,
        "cited": cited,
        "citations": citations[:5],
        "answer_snippet": answer[:200],
    }


def main() -> int:
    api_key = os.getenv("PERPLEXITY_API_KEY", "").strip()
    if not api_key:
        print("❌ PERPLEXITY_API_KEY が未設定です。")
        print("   https://www.perplexity.ai/settings/api で取得して .env に設定してください。")
        return 1

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    cited_count = 0
    total = len(QUERIES)

    print(f"=== 知リポAI Perplexity 引用チェック ({now}) ===")
    print(f"チェック対象サイト: {SITE_URL}")
    print()

    results = []
    for q in QUERIES:
        print(f"  検索: {q!r} ... ", end="", flush=True)
        result = _check_citation(q, api_key)
        result["checked_at"] = now
        results.append(result)
        if result.get("error"):
            print(f"エラー: {result['error']}")
        elif result["cited"]:
            cited_count += 1
            print("✅ 引用あり")
        else:
            print("— 引用なし")

    print()
    print(f"結果: {cited_count}/{total} クエリで引用")
    if cited_count == 0:
        print("⚠️  引用が確認できませんでした。GEO施策を継続してください。")
    elif cited_count >= total // 2:
        print("✅ 半数以上のクエリで引用されています。")

    with LOG_FILE.open("a", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\nログ保存: {LOG_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
