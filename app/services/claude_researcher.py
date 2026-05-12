"""Claude Code CLI を subprocess で呼び出してウェブリサーチを実行するサービス

ニュースと論文を並列で 2 プロセス同時実行することで、タイムアウトを防ぐ。

動作要件:
  - Claude Code CLI (npm i -g @anthropic-ai/claude-code) がインストール済み
  - claude login 済み（OAuth または ANTHROPIC_API_KEY 設定済み）
  - Render 本番環境では自動スキップ（claude CLI がないため）

Windows では PATH 上の node_modules\\...\\bin\\claude.exe が先に拾われ非互換になることがあるため、
既定では %APPDATA%\\npm\\claude.cmd（npm シム）を優先する。上書きは環境変数 CLAUDE_CODE_CMD にフルパス。
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
JST = ZoneInfo("Asia/Tokyo")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CURATED_FILE = PROJECT_ROOT / "curated_articles.json"
_usage_lock = threading.Lock()
_usage_stats: dict[str, dict[str, float]] = {}
_claude_json_repair_lock = threading.Lock()


def _claude_user_config_path() -> Path:
    return Path.home() / ".claude.json"


def _claude_backups_dir() -> Path:
    return Path.home() / ".claude" / "backups"


def _is_valid_json_file(path: Path) -> bool:
    try:
        if not path.is_file():
            return False
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return False
        json.loads(raw)
        return True
    except Exception:
        return False


def repair_claude_user_config_if_corrupted() -> bool:
    """~/.claude.json が壊れている場合、.claude/backups の最新正常バックアップで復元する。"""
    with _claude_json_repair_lock:
        cfg = _claude_user_config_path()
        if _is_valid_json_file(cfg):
            return True
        backups_dir = _claude_backups_dir()
        if not backups_dir.is_dir():
            logger.warning(
                ".claude.json が無効ですが backups がありません (%s)。Claude Code の再インストールや claude login を確認してください。",
                backups_dir,
            )
            return False
        candidates = sorted(
            backups_dir.glob(".claude.json.backup.*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for bp in candidates:
            if not _is_valid_json_file(bp):
                continue
            try:
                shutil.copy2(bp, cfg)
                logger.info("破損した .claude.json をバックアップから復元しました: %s", bp.name)
                return True
            except Exception as e:
                logger.warning(".claude.json 復元コピーに失敗 (%s): %s", bp, e)
        logger.warning(
            "有効な .claude.json バックアップが見つかりません。Claude CLI の案内に従い手動復元するか、claude login をやり直してください。"
        )
        return False


_NEWS_PROMPT = """\
今日は {today}（日本時間）です。
以下の手順でリサーチし、「知リポAI」ニュースサイト（20〜40代の知的好奇心が高い日本語読者向け）に
ふさわしい【ニュース記事のみ】を {n} 件選定して、
{output_file} に以下の JSON 形式で書き込んでください（配列のみ、説明文不要）。

[
  {{
    "title": "タイトル（日本語）",
    "url": "実在する記事の URL",
    "summary": "100〜150 字の日本語要約",
    "source": "媒体名",
    "category": "テクノロジー|国際|国内|政治・社会|エンタメ|スポーツ のいずれか",
    "published": "YYYY-MM-DDTHH:MM:SS",
    "image_url": null
  }}
]

【Step 1: トレンド収集（必須）】
まず以下を検索してトレンドキーワードを把握する:
- 「Google トレンド 急上昇 日本 {today}」
- 「X（Twitter）トレンド 日本 {today}」
- 「急上昇ワード {today}」
→ 急上昇中のキーワードを10個以上リストアップする

【Step 2: トレンドに沿ったニュース検索】
Step 1 で集めたキーワードをもとにニュースを検索し、
「なぜ今話題なのか」が分かる記事を優先的に探す

【Step 3: 選定基準で絞り込む】
- 日本のニュース約 70% / 海外ニュース約 30%
- 今まさに急上昇・バズっている話題性の高いニュースを最優先
- 速報・Breaking News を積極的に選ぶ（鮮度重視）
- 「今これが話題になっているんだ」と感じさせる旬の記事を選ぶ
- 訃報・芸能ゴシップ・選挙速報は除外
- URL は必ず実在するニュース記事の URL（架空 URL 禁止）
- カテゴリは テクノロジー / 国内 / 国際 / 政治・社会 / エンタメ / スポーツ から選ぶ

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
    "title": "タイトルは必ず日本語で書いてください（英語論文も日本語訳する）",
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


def _estimate_tokens(text: str) -> int:
    # 日本語は 1token あたり文字数が揺れるため、簡易に 1token ~= 3.2 chars で概算
    t = text or ""
    return max(1, int(len(t) / 3.2))


def _record_usage(kind: str, *, prompt: str, output: str, elapsed_sec: float, ok: bool) -> None:
    in_tok = _estimate_tokens(prompt)
    out_tok = _estimate_tokens(output)
    with _usage_lock:
        cur = _usage_stats.get(kind) or {
            "calls": 0.0,
            "success_calls": 0.0,
            "failed_calls": 0.0,
            "input_tokens_est": 0.0,
            "output_tokens_est": 0.0,
            "elapsed_sec_total": 0.0,
            "last_elapsed_sec": 0.0,
            "last_input_tokens_est": 0.0,
            "last_output_tokens_est": 0.0,
        }
        cur["calls"] += 1
        if ok:
            cur["success_calls"] += 1
        else:
            cur["failed_calls"] += 1
        cur["input_tokens_est"] += in_tok
        cur["output_tokens_est"] += out_tok
        cur["elapsed_sec_total"] += max(0.0, float(elapsed_sec))
        cur["last_elapsed_sec"] = max(0.0, float(elapsed_sec))
        cur["last_input_tokens_est"] = in_tok
        cur["last_output_tokens_est"] = out_tok
        _usage_stats[kind] = cur
    logger.info(
        "CLAUDE_USAGE kind=%s ok=%s in_tok~%d out_tok~%d elapsed=%.2fs",
        kind,
        ok,
        in_tok,
        out_tok,
        elapsed_sec,
    )


def get_claude_usage_stats() -> dict:
    with _usage_lock:
        # JSONで返しやすいようにコピーして整数化
        snap = {}
        for k, v in _usage_stats.items():
            snap[k] = {
                "calls": int(v.get("calls", 0)),
                "success_calls": int(v.get("success_calls", 0)),
                "failed_calls": int(v.get("failed_calls", 0)),
                "input_tokens_est": int(v.get("input_tokens_est", 0)),
                "output_tokens_est": int(v.get("output_tokens_est", 0)),
                "elapsed_sec_total": round(float(v.get("elapsed_sec_total", 0.0)), 2),
                "last_elapsed_sec": round(float(v.get("last_elapsed_sec", 0.0)), 2),
                "last_input_tokens_est": int(v.get("last_input_tokens_est", 0)),
                "last_output_tokens_est": int(v.get("last_output_tokens_est", 0)),
            }
        return snap


def run_claude_text_gen(prompt: str, timeout: int = 120, usage_kind: str = "text_gen") -> str:
    """Claude CLI にプロンプトを渡してテキストを生成する（記事リサーチ以外の汎用用途）。
    出力をファイルに書かせて読み返す方式。失敗・タイムアウト時は空文字を返す。"""
    repair_claude_user_config_if_corrupted()
    cmd_path = _find_claude_cmd()
    if not cmd_path:
        _record_usage(usage_kind, prompt=prompt, output="", elapsed_sec=0.0, ok=False)
        return ""
    with tempfile.TemporaryDirectory() as tmpdir:
        out_file = Path(tmpdir) / "out.txt"
        slash_path = str(out_file).replace("\\", "/")
        full_prompt = (
            f"{prompt}\n\n"
            f"上記の内容を {slash_path} に書き込んでください。"
            "他のファイルへの書き込みや Web 検索は不要です。"
        )
        cmd = _build_cmd_text_only(cmd_path)
        started = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd,
                input=full_prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
                cwd=str(PROJECT_ROOT),
                env=os.environ.copy(),
                shell=False,
            )
            if proc.returncode != 0:
                logger.warning(
                    "run_claude_text_gen 失敗 code=%d stderr=%s",
                    proc.returncode,
                    (proc.stderr or "")[:300],
                )
                _record_usage(
                    usage_kind,
                    prompt=full_prompt,
                    output=(proc.stdout or "")[:5000],
                    elapsed_sec=time.perf_counter() - started,
                    ok=False,
                )
                return ""
            if out_file.exists():
                out = out_file.read_text(encoding="utf-8").strip()
                _record_usage(
                    usage_kind,
                    prompt=full_prompt,
                    output=out,
                    elapsed_sec=time.perf_counter() - started,
                    ok=True,
                )
                return out
            out = (proc.stdout or "").strip()
            _record_usage(
                usage_kind,
                prompt=full_prompt,
                output=out,
                elapsed_sec=time.perf_counter() - started,
                ok=True,
            )
            return out
        except subprocess.TimeoutExpired:
            logger.warning("run_claude_text_gen タイムアウト (%d 秒)", timeout)
            _record_usage(
                usage_kind,
                prompt=full_prompt,
                output="",
                elapsed_sec=time.perf_counter() - started,
                ok=False,
            )
            return ""
        except Exception as e:
            logger.warning("run_claude_text_gen エラー: %s", e)
            _record_usage(
                usage_kind,
                prompt=full_prompt,
                output="",
                elapsed_sec=time.perf_counter() - started,
                ok=False,
            )
            return ""


def _npm_claude_cmd_paths() -> list[Path]:
    """Windows で Node 経由の公式 npm シム候補（優先順）。"""
    out: list[Path] = []
    for key in ("APPDATA", "LOCALAPPDATA"):
        root = os.environ.get(key, "").strip()
        if root:
            p = Path(root) / "npm" / "claude.cmd"
            if p.is_file():
                out.append(p)
    return out


def _is_windows_packaged_claude_exe(path: str) -> bool:
    """PATH が先に拾うパッケージ内 claude.exe（環境によっては非互換）かどうか。"""
    low = path.replace("/", "\\").lower()
    if not low.endswith("\\claude.exe"):
        return False
    return "node_modules" in low and "claude-code" in low


def _find_claude_cmd() -> str | None:
    override = os.environ.get("CLAUDE_CODE_CMD", "").strip()
    if override:
        o = Path(override)
        if o.is_file():
            return str(o.resolve())
        w = shutil.which(override)
        if w:
            return w

    if sys.platform == "win32":
        for p in _npm_claude_cmd_paths():
            return str(p.resolve())
        found_cmd = shutil.which("claude.cmd")
        if found_cmd:
            return found_cmd
        for candidate in ("claude",):
            found = shutil.which(candidate)
            if found and not _is_windows_packaged_claude_exe(found):
                return found
        return None

    for candidate in ("claude", "claude.cmd"):
        found = shutil.which(candidate)
        if found:
            return found
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


def _build_cmd_text_only(cmd_path: str) -> list[str]:
    """テキスト生成用: Write ツールのみ（WebSearch 不要でコスト・時間を節約）"""
    base = [
        "--dangerously-skip-permissions",
        "--allowed-tools", "Write",
        "-p",
        "--input-format", "text",
    ]
    if sys.platform == "win32":
        return ["cmd", "/c", cmd_path] + base
    return [cmd_path] + base


def _run_one(label: str, prompt: str, output_file: Path, timeout: int, result: dict) -> None:
    """単一の Claude サブプロセスを実行し、result[label] にパース済みリストを格納する。"""
    repair_claude_user_config_if_corrupted()
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

        raw = ""
        if output_file.exists():
            raw = output_file.read_text(encoding="utf-8").strip()
        else:
            # 終了0でも Write 先を誤る・ツールを使わず stdout のみ、などでファイルが無いことがある
            stdout = (proc.stdout or "").strip()
            stderr_tail = (proc.stderr or "")[-800:]
            logger.warning(
                "[%s] 指定パスに出力なし。stdout から JSON 配列を救済します (stderr末尾=%r)",
                label,
                stderr_tail,
            )
            raw = stdout
            if "```" in raw:
                raw = "\n".join(l for l in raw.splitlines() if not l.strip().startswith("```")).strip()
            if raw and not raw.lstrip().startswith("["):
                i, j = raw.find("["), raw.rfind("]")
                if i != -1 and j != -1 and j > i:
                    raw = raw[i : j + 1].strip()

        if not raw:
            logger.error("[%s] 指定ファイルにも stdout にも有効な JSON がありませんでした", label)
            return

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
