"""Claude Code CLI を subprocess で呼び出してウェブリサーチを実行するサービス

ニュースと論文を 1 回の Claude セッションでまとめてリサーチし、重複 Web 検索を避ける。

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


_COMBINED_PROMPT = """\
今日は {today}（日本時間）です。
Web 検索はこの依頼の中で効率よくまとめて行い、同じトレンド調査を何度も繰り返さないでください。

「知リポAI」ニュースサイト（20〜40代の知的好奇心が高い日本語読者向け）向けに、
ニュース {n_news} 件と学術論文 {n_papers} 件を選定し、
{output_file} に次の JSON オブジェクトだけを書き込んでください（説明文・Markdown のコードフェンス禁止）。
キー名は必ず半角の "news" と "papers" を使ってください。

{{
  "news": [
    {{
      "title": "タイトル（日本語）",
      "url": "実在する記事の URL",
      "summary": "400〜600 字の日本語要約（何が起きたか・なぜ重要か・主要な数字や固有名詞を必ず含める）",
      "source": "媒体名",
      "category": "テクノロジー|国際|国内|政治・社会|エンタメ|スポーツ のいずれか",
      "published": "YYYY-MM-DDTHH:MM:SS",
      "image_url": null
    }}
  ],
  "papers": [
    {{
      "title": "タイトルは必ず日本語で書いてください（英語論文も日本語訳する）",
      "url": "実在する論文の URL",
      "summary": "400〜600 字の日本語要約（何が起きたか・なぜ重要か・主要な数字や固有名詞を必ず含める）",
      "source": "媒体名（arXiv / Nature / PubMed など）",
      "category": "研究・論文",
      "published": "YYYY-MM-DDTHH:MM:SS",
      "image_url": null
    }}
  ]
}}

【Step 1: トレンド収集（この依頼で1回だけ）】
以下を検索し、急上昇キーワードを10個以上リストアップする（Step 2 のニュース選定にのみ使う）:
- 「Google トレンド 急上昇 日本 {today}」
- 「X（Twitter）トレンド 日本 {today}」
- 「急上昇ワード {today}」

【Step 2: ニュース】news にちょうど {n_news} 件
Step 1 のキーワードをもとにニュースを検索し、「なぜ今話題なのか」が分かる記事を優先する。
- 日本のニュース約 70% / 海外ニュース約 30%
- 急上昇・バズ・鮮度を最優先。訃報・芸能ゴシップ・選挙速報は除外
- URL は必ず実在するニュース記事（架空禁止）。記事本文が読めるURLのみ（要約だけで済まない薄いソースは選ばない）
- summary は 400〜600 字。見出しの言い換えだけは不可。5W1H・背景・影響の骨子まで書く
- カテゴリは テクノロジー / 国内 / 国際 / 政治・社会 / エンタメ / スポーツ

【Step 3: 論文】papers にちょうど {n_papers} 件
- 海外の英語論文を重視（papers の過半数を英語論文にする）
- テーマ例: 健康・長寿・睡眠・メンタル / AI・宇宙・量子 / スポーツ科学・栄養 / 行動経済学 / 気候・環境
- arXiv / PubMed / Nature / Science / bioRxiv / medRxiv など。カテゴリは必ず「研究・論文」
- URL は必ず実在する論文（架空禁止）。abstract や本文が読めるページ（PDF直リンクのみは避け abs ページを優先）
- summary は 400〜600 字。研究目的・方法・主要な結果・なぜ重要かを日本語で具体的に

【ニュースの使用禁止メディア（ペイウォール）】
- bloomberg.com / wsj.com / ft.com / nytimes.com / economist.com

【ニュースの優先メディア】
- nhk.or.jp / reuters.com / apnews.com / afpbb.com / techcrunch.com / theverge.com / bbc.com / cnn.com / japantimes.co.jp

【論文の優先ソース】
- arxiv.org / pubmed.ncbi.nlm.nih.gov / nature.com / science.org / biorxiv.org / medrxiv.org / sciencedaily.com

上記オブジェクトを {output_file} に書き込んで完了してください。
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


def _invoke_claude_research_session(
    label: str, prompt: str, output_file: Path, timeout: int
) -> str | None:
    """Claude を1回起動し、output_file または stdout から JSON テキストを返す。失敗時は None。"""
    repair_claude_user_config_if_corrupted()
    cmd_path = _find_claude_cmd()
    if not cmd_path:
        logger.error("[%s] claude コマンドが見つかりません", label)
        return None

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
            return None

        raw = ""
        if output_file.exists():
            raw = output_file.read_text(encoding="utf-8").strip()
        else:
            stdout = (proc.stdout or "").strip()
            stderr_tail = (proc.stderr or "")[-800:]
            logger.warning(
                "[%s] 指定パスに出力なし。stdout から JSON を救済します (stderr末尾=%r)",
                label,
                stderr_tail,
            )
            raw = stdout
            if "```" in raw:
                raw = "\n".join(l for l in raw.splitlines() if not l.strip().startswith("```")).strip()
            if raw:
                s = raw.lstrip()
                if not s.startswith("{") and not s.startswith("["):
                    i, j = raw.find("{"), raw.rfind("}")
                    if i != -1 and j != -1 and j > i:
                        raw = raw[i : j + 1].strip()
                    else:
                        i, j = raw.find("["), raw.rfind("]")
                        if i != -1 and j != -1 and j > i:
                            raw = raw[i : j + 1].strip()

        if not raw:
            logger.error("[%s] 指定ファイルにも stdout にも有効な JSON がありませんでした", label)
            return None

        if raw.startswith("```"):
            raw = "\n".join(l for l in raw.splitlines() if not l.startswith("```")).strip()

        return raw if raw else None

    except subprocess.TimeoutExpired:
        logger.error("[%s] タイムアウト (%d 秒)", label, timeout)
        return None
    except Exception as e:
        logger.error("[%s] 予期しないエラー: %s", label, e)
        return None


def _parse_curated_research_json(raw: str) -> tuple[list, int, int]:
    """JSON をパースし、(マージ済みリスト, ニュース件数, 論文件数) を返す。"""
    data = json.loads(raw)
    if isinstance(data, list):
        if not data:
            raise ValueError("空のリスト")
        return data, len(data), 0
    if isinstance(data, dict):
        news = data.get("news") or data.get("ニュース") or []
        papers = data.get("papers") or data.get("論文") or []
        if not isinstance(news, list):
            news = []
        if not isinstance(papers, list):
            papers = []
        merged = news + papers
        if not merged:
            raise ValueError("news/papers がともに空")
        return merged, len(news), len(papers)
    raise ValueError("JSON はオブジェクトまたは配列である必要があります")


def run_claude_research(n: int = 15, n_news: int = 8, n_papers: int = 7, timeout: int = 900) -> bool:
    """
    Claude を1回だけ起動し、ニュースと論文をまとめてリサーチして curated_articles.json を更新する。

    n は呼び出し互換のため残す（n_news + n_papers と揃える想定）。戻り値: 1件以上取得できれば True。
    """
    _ = n

    today = datetime.now(JST).strftime("%Y-%m-%d")

    with tempfile.TemporaryDirectory() as tmpdir:
        out_file = Path(tmpdir) / "curated_batch.json"
        prompt = _COMBINED_PROMPT.format(
            today=today,
            n_news=n_news,
            n_papers=n_papers,
            output_file=str(out_file).replace("\\", "/"),
        )
        raw = _invoke_claude_research_session("ニュース+論文", prompt, out_file, timeout)
        if not raw:
            return False
        try:
            all_articles, n_news_ok, n_papers_ok = _parse_curated_research_json(raw)
        except (json.JSONDecodeError, ValueError) as e:
            logger.error("Claude リサーチ JSON の解釈に失敗: %s", e)
            return False

    if not all_articles:
        logger.error("ニュース・論文ともに取得できませんでした")
        return False

    CURATED_FILE.write_text(
        json.dumps(all_articles, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(
        "Claude リサーチ完了: 合計 %d 件 (ニュース %d / 論文 %d) を保存",
        len(all_articles),
        n_news_ok,
        n_papers_ok,
    )
    return True
