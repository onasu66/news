"""Claude Code CLI を subprocess で呼び出してウェブリサーチを実行するサービス

ニュースと論文を並列で 2 プロセス同時実行することで、タイムアウトを防ぐ。

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
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
JST = ZoneInfo("Asia/Tokyo")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CURATED_FILE = PROJECT_ROOT / "curated_articles.json"

_NEWS_PROMPT = """\
今日は {today}（日本時間）です。
Google 検索と X のトレンドを調べ、「知リポAI」ニュースサイト（20〜40代の知的好奇心が高い日本語読者向け）に
ふさわしい【ニュース記事のみ】を {n} 件リサーチして選定し、
{output_file} に以下の JSON 形式で書き込んでください（配列のみ、説明文不要）。

[
  {{
    "title": "タイトル（日本語可）",
    "url": "実在する記事の URL",
    "summary": "100〜150 字の日本語要約",
    "source": "媒体名",
    "category": "テクノロジー|国際|国内|政治・社会|エンタメ|スポーツ のいずれか",
    "published": "YYYY-MM-DDTHH:MM:SS",
    "image_url": null
  }}
]

【選定基準】
- 日本のニュース約 70% / 海外ニュース約 30%
- 今 X・Google で話題になっているトレンドに合致するものを最優先
- 「へえ、そうなんだ」と思わせる知的好奇心をくすぐるニュースを選ぶ
- カテゴリは テクノロジー / 国内 / 国際 / 政治・社会 / エンタメ / スポーツ から選ぶ
- スポーツ速報・訃報・芸能ゴシップ・選挙速報は除外
- URL は必ず実在するニュース記事の URL（架空 URL 禁止）

【使用禁止メディア（ペイウォール）】
- bloomberg.com / wsj.com / ft.com / nytimes.com / economist.com

【優先メディア】
- nhk.or.jp / reuters.com / apnews.com / afpbb.com / techcrunch.com / theverge.com / bbc.com / cnn.com / japantimes.co.jp

{n} 件の JSON を {output_file} に書き込んで作業を完了してください。
"""

_PAPERS_PROMPT = """\
今日は {today}（日本時間）です。
「知リポAI」ニュースサイト（20〜40代の知的好奇心が高い日本語読者向け）向けに
【学術論文のみ】を {n} 件リサーチして選定し、
{output_file} に以下の JSON 形式で書き込んでください（配列のみ、説明文不要）。

[
  {{
    "title": "タイトル（日本語可）",
    "url": "実在する論文の URL",
    "summary": "100〜150 字の日本語要約",
    "source": "媒体名（arXiv / Nature / PubMed など）",
    "category": "研究・論文",
    "published": "YYYY-MM-DDTHH:MM:SS",
    "image_url": null
  }}
]

【選定基準】
- 海外の英語論文を重視（{n} 件中 8 件以上）
- 「皆が気になる・検索されやすい」テーマを優先:
    健康・長寿・ダイエット・睡眠・メンタルヘルス
    AI・テクノロジー・ロボット / 宇宙・物理・量子
    筋トレ・スポーツ科学・栄養 / 経済・行動経済学 / 気候変動・環境
- arXiv / PubMed / Nature / Science / bioRxiv / medRxiv などの論文
- カテゴリは必ず「研究・論文」
- URL は必ず実在する論文の URL（架空 URL 禁止）

【優先ソース】
- arxiv.org / pubmed.ncbi.nlm.nih.gov / nature.com / science.org / biorxiv.org / medrxiv.org / sciencedaily.com

{n} 件の JSON を {output_file} に書き込んで作業を完了してください。
"""


def is_claude_available() -> bool:
    if os.environ.get("RENDER", "").strip().lower() == "true":
        return False
    return _find_claude_cmd() is not None


def _find_claude_cmd() -> str | None:
    for candidate in ("claude", "claude.cmd"):
        found = shutil.which(candidate)
        if found:
            return candidate
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        npm_cmd = Path(appdata) / "npm" / "claude.cmd"
        if npm_cmd.exists():
            return str(npm_cmd)
    return None


def _build_cmd(cmd_path: str) -> list[str]:
    base = [
        "--dangerously-skip-permissions",
        "--allowed-tools", "WebSearch,Write",
        "-p",
        "--input-format", "text",
    ]
    if sys.platform == "win32":
        return ["cmd", "/c", cmd_path] + base
    return [cmd_path] + base


def _run_one(label: str, prompt: str, output_file: Path, timeout: int, result: dict) -> None:
    """単一の Claude サブプロセスを実行し、result[label] にパース済みリストを格納する。"""
    cmd_path = _find_claude_cmd()
    if not cmd_path:
        logger.error("[%s] claude コマンドが見つかりません", label)
        return

    cmd = _build_cmd(cmd_path)
    logger.info("[%s] Claude 起動 (タイムアウト=%d 秒)", label, timeout)

    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
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
                "[%s] 終了コード %d:\nstdout=%s\nstderr=%s",
                label,
                proc.returncode,
                (proc.stdout or "")[:500],
                (proc.stderr or "")[:500],
            )
            return

        if not output_file.exists():
            logger.error("[%s] 出力ファイルが書き込まれませんでした", label)
            return

        raw = output_file.read_text(encoding="utf-8").strip()
        if raw.startswith("```"):
            raw = "\n".join(l for l in raw.splitlines() if not l.startswith("```")).strip()

        data = json.loads(raw)
        if not isinstance(data, list) or not data:
            raise ValueError("空またはリストでない")

        result[label] = data
        logger.info("[%s] 完了: %d 件", label, len(data))

    except subprocess.TimeoutExpired:
        logger.error("[%s] タイムアウト (%d 秒)", label, timeout)
    except json.JSONDecodeError as e:
        logger.error("[%s] JSON パースエラー: %s", label, e)
    except Exception as e:
        logger.error("[%s] 予期しないエラー: %s", label, e)


def run_claude_research(n: int = 15, n_news: int = 8, n_papers: int = 7, timeout: int = 900) -> bool:
    """
    ニュースと論文を並列 2 プロセスでリサーチし curated_articles.json を更新する。

    並列実行により合計時間を約 1/2 に短縮できる。
    戻り値: ニュース・論文のどちらか一方でも取得できれば True
    """
    cmd_path = _find_claude_cmd()
    if not cmd_path:
        logger.warning("claude コマンドが見つかりません。Claude Code CLI をインストールしてください。")
        return False

    today = datetime.now(JST).strftime("%Y-%m-%d")
    result: dict[str, list] = {}

    with tempfile.TemporaryDirectory() as tmpdir:
        news_file = Path(tmpdir) / "news.json"
        papers_file = Path(tmpdir) / "papers.json"

        news_prompt = _NEWS_PROMPT.format(
            today=today, n=n_news,
            output_file=str(news_file).replace("\\", "/"),
        )
        papers_prompt = _PAPERS_PROMPT.format(
            today=today, n=n_papers,
            output_file=str(papers_file).replace("\\", "/"),
        )

        threads = [
            threading.Thread(
                target=_run_one,
                args=("ニュース", news_prompt, news_file, timeout, result),
                daemon=True,
            ),
            threading.Thread(
                target=_run_one,
                args=("論文", papers_prompt, papers_file, timeout, result),
                daemon=True,
            ),
        ]

        logger.info("ニュース・論文を並列リサーチ開始 (各タイムアウト=%d 秒)", timeout)
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout + 60)

        all_articles = result.get("ニュース", []) + result.get("論文", [])

    if not all_articles:
        logger.error("ニュース・論文ともに取得できませんでした")
        return False

    CURATED_FILE.write_text(
        json.dumps(all_articles, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("Claude リサーチ完了: 合計 %d 件 (ニュース %d / 論文 %d) を保存",
                len(all_articles),
                len(result.get("ニュース", [])),
                len(result.get("論文", [])))
    return True
