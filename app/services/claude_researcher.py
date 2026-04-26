"""Claude Code CLI を subprocess で呼び出してウェブリサーチを実行するサービス

APScheduler から呼ぶことで、定時に Claude が自律的に Web 検索・X トレンドを
調べて curated_articles.json を更新し、既存パイプラインで記事化する。

動作要件:
  - Claude Code CLI (npm i -g @anthropic-ai/claude-code) がインストール済み
  - claude login 済み（OAuth または ANTHROPIC_API_KEY 設定済み）
  - Render 本番環境では自動スキップ（claude CLI がないため）
"""
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
JST = ZoneInfo("Asia/Tokyo")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CURATED_FILE = PROJECT_ROOT / "curated_articles.json"

_PROMPT_TEMPLATE = """\
今日は {today}（日本時間）です。
Google 検索と X のトレンドを調べ、「知リポAI」ニュースサイト（20〜40代の知的好奇心が高い日本語読者向け）に
ふさわしい最新ニュース・論文を {n} 件リサーチして選定し、
{curated_file} に以下の JSON 形式で書き込んでください（既存ファイルを上書き）。

JSON 形式（配列のみ、説明文不要）:
[
  {{
    "title": "タイトル（日本語可）",
    "url": "実在する記事の URL",
    "summary": "100〜150 字の日本語要約",
    "source": "媒体名",
    "category": "テクノロジー|国際|国内|政治・社会|研究・論文|エンタメ|スポーツ のいずれか",
    "published": "YYYY-MM-DDTHH:MM:SS",
    "image_url": null
  }}
]

選定基準:
- 今話題のトレンドキーワードに関連する記事を最優先
- 日本関連 70% ／ 海外 30%
- ジャンルを分散（テクノロジー・経済・科学・政策・論文など）
- 研究・論文はカテゴリを必ず「研究・論文」にする
- スポーツ速報・訃報・芸能ゴシップは除外
- URL は必ず実在する記事の URL（架空 URL 禁止）

【重要】以下のメディアは本文取得が403エラーになるため選ばないこと:
- bloomberg.com（有料・ペイウォール）
- wsj.com（有料・ペイウォール）
- ft.com（有料・ペイウォール）
- nytimes.com（有料・ペイウォール）
- economist.com（有料・ペイウォール）
代わりに、以下のような無料で本文を読めるメディアを優先すること:
- nhk.or.jp / www3.nhk.or.jp
- reuters.com / apnews.com / afpbb.com
- techcrunch.com / theverge.com / wired.com
- nikkei.com（見出し記事は可）/ japan-times.co.jp
- arxiv.org / nature.com（オープンアクセス論文）
- nasa.gov / jpl.nasa.gov / sciencedaily.com
- bbc.com / cnn.com / reuters.com

{n} 件の JSON を {curated_file} に書き込んで作業を完了してください。
"""


def is_claude_available() -> bool:
    """Claude Code CLI が使える環境かどうかを返す。Render 本番では False。"""
    # Render 本番は claude CLI がないため自動スキップ
    if os.environ.get("RENDER", "").strip().lower() == "true":
        return False
    return _find_claude_cmd() is not None


def _find_claude_cmd() -> str | None:
    """claude コマンドのパスを返す。見つからなければ None。"""
    for candidate in ("claude", "claude.cmd"):
        found = shutil.which(candidate)
        if found:
            return candidate
    # Windows npm グローバルの典型パスを直接確認
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        npm_cmd = Path(appdata) / "npm" / "claude.cmd"
        if npm_cmd.exists():
            return str(npm_cmd)
    return None


def run_claude_research(n: int = 15, timeout: int = 480) -> bool:
    """
    Claude Code CLI を使って Web リサーチを行い curated_articles.json を更新する。

    n       : 選定する記事数
    timeout : タイムアウト秒数（デフォルト 8 分）
    戻り値  : 成功すれば True、失敗すれば False
    """
    cmd_path = _find_claude_cmd()
    if not cmd_path:
        logger.warning("claude コマンドが見つかりません。Claude Code CLI をインストールしてください。")
        return False

    today = datetime.now(JST).strftime("%Y-%m-%d")
    prompt = _PROMPT_TEMPLATE.format(
        today=today,
        n=n,
        curated_file=str(CURATED_FILE).replace("\\", "/"),
    )

    # プロンプトは stdin 経由で渡す（Windows で | などのシェルメタ文字を含むと
    # cmd.exe に誤解釈されるため、コマンドライン引数には入れない）
    base_cmd = [
        "--dangerously-skip-permissions",
        "--allowed-tools", "WebSearch,Write",
        "--max-budget-usd", "0.80",
        "-p",          # stdin から読む（positional prompt なし）
        "--input-format", "text",
    ]
    # Windows では .cmd ファイルを cmd /c でラップしないと実行できない
    if sys.platform == "win32":
        cmd = ["cmd", "/c", cmd_path] + base_cmd
    else:
        cmd = [cmd_path] + base_cmd

    logger.info("Claude リサーチ開始: %d 件 (タイムアウト=%d 秒)", n, timeout)

    try:
        proc = subprocess.run(
            cmd,
            input=prompt,          # stdin にプロンプトを流す
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            cwd=str(PROJECT_ROOT),
            env=os.environ.copy(),
            shell=False,
        )

        if proc.returncode != 0:
            logger.error(
                "Claude 終了コード %d:\nstdout=%s\nstderr=%s",
                proc.returncode,
                (proc.stdout or "")[:300],
                (proc.stderr or "")[:300],
            )
            return False

        # 書き込まれた JSON を検証
        if not CURATED_FILE.exists():
            logger.error("curated_articles.json が書き込まれませんでした")
            return False

        raw = CURATED_FILE.read_text(encoding="utf-8").strip()
        # Claude が JSON ブロック（```json ... ```）で返した場合に対応
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(
                l for l in lines if not l.startswith("```")
            ).strip()

        data = json.loads(raw)
        if not isinstance(data, list) or not data:
            raise ValueError("空またはリストでない")

        # 検証済みの JSON で上書き（クリーニング済み）
        CURATED_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Claude リサーチ完了: %d 件を curated_articles.json に保存", len(data))
        return True

    except subprocess.TimeoutExpired:
        logger.error("Claude リサーチがタイムアウト (%d 秒)", timeout)
        return False
    except json.JSONDecodeError as e:
        logger.error("生成された curated_articles.json が不正な JSON: %s", e)
        return False
    except Exception as e:
        logger.error("Claude リサーチで予期しないエラー: %s", e)
        return False
