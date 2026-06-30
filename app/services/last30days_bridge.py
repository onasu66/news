"""last30days.py を Python から直接実行してトレンドキーワードを取得する

Claude CLI 経由ではなく Python subprocess で直接呼び出すことで、
Claude のターン消費なしにエンゲージメント上位のキーワードを収集できる。

X の AUTH_TOKEN/CT0 が設定済みの場合は X 検索も自動で使われる。
"""
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_SKILL_SCRIPT = (
    Path.home() / ".claude" / "skills" / "last30days" / "scripts" / "last30days.py"
)


def is_available() -> bool:
    """last30days.py と Python 3.12+ が両方使える場合に True。"""
    if not _SKILL_SCRIPT.is_file():
        return False
    return _find_python() is not None


def _find_python() -> str | None:
    """Python 3.12+ 実行コマンドを返す。なければ None。"""
    # スキル専用 venv を最優先
    venv_py = _SKILL_SCRIPT.parent.parent / ".venv" / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )
    if venv_py.exists() and _ver_ok(str(venv_py)):
        return str(venv_py)

    for name in ("python3.14", "python3.13", "python3.12", "python3"):
        import shutil
        p = shutil.which(name)
        if p and _ver_ok(p):
            return p

    if sys.platform == "win32":
        for ver in ("3.14", "3.13", "3.12"):
            try:
                r = subprocess.run(
                    ["py", f"-{ver}", "--version"],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0:
                    return f"py -{ver}"
            except Exception:
                pass
    return None


def _ver_ok(cmd: str) -> bool:
    try:
        r = subprocess.run(
            [cmd, "--version"], capture_output=True, text=True, timeout=5,
        )
        m = re.search(r"Python (\d+)\.(\d+)", r.stdout + r.stderr)
        if m and (int(m.group(1)), int(m.group(2))) >= (3, 12):
            return True
    except Exception:
        pass
    return False


def _build_env() -> dict:
    env = os.environ.copy()
    env_file = Path.home() / ".config" / "last30days" / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip("\"'")
            if k and v and not env.get(k):
                env[k] = v
    if not env.get("EXCLUDE_SOURCES"):
        env["EXCLUDE_SOURCES"] = "tiktok,instagram,threads,pinterest,perplexity"
    return env


def _extract_keywords_from_output(output: str) -> list[str]:
    """last30days のコンパクト出力から Evidence Cluster タイトルを抽出してキーワードリストを返す。"""
    keywords: list[str] = []
    seen: set[str] = set()

    # パターン1: "### 1. Cluster Title (score N, M items, ...)"
    for m in re.finditer(r"###\s+\d+\.\s+(.+?)\s*\(score", output):
        kw = m.group(1).strip()
        kl = kw.lower()
        if kw and kl not in seen and 3 <= len(kw) <= 60:
            seen.add(kl)
            keywords.append(kw)

    # パターン2: Reddit/X 投稿タイトル（クラスタ未抽出の場合の補完）
    if len(keywords) < 3:
        for m in re.finditer(
            r"\[(?:reddit|x)\]\s+(.+?)(?:\s*\(r/|\s*score:|\s*\[)", output
        ):
            kw = m.group(1).strip()
            kl = kw.lower()
            if kw and kl not in seen and 5 <= len(kw) <= 60:
                seen.add(kl)
                keywords.append(kw)
                if len(keywords) >= 15:
                    break

    return keywords


def fetch_trending_keywords(
    queries: list[str] | None = None,
    last_days: int = 2,
    max_keywords: int = 15,
    timeout: int = 90,
) -> list[str]:
    """
    last30days.py を直接実行してトレンドキーワードリストを返す。

    queries    : 検索クエリリスト（省略時はデフォルト日本語クエリを使用）
    last_days  : 何日前まで遡るか（デフォルト 2 日）
    max_keywords: 返すキーワードの最大件数
    timeout    : 1 クエリあたりのタイムアウト秒数

    失敗・未インストール時は空リストを返す（例外を上げない）。
    """
    if not _SKILL_SCRIPT.is_file():
        logger.debug("last30days.py が未インストール: %s", _SKILL_SCRIPT)
        return []

    py = _find_python()
    if not py:
        logger.debug("Python 3.12+ が見つかりません")
        return []

    default_queries = [
        "日本 ニュース 話題 今日",
        "テクノロジー AI 最新 話題",
    ]
    actual_queries = (queries or default_queries)[:2]

    all_keywords: list[str] = []
    seen: set[str] = set()
    env = _build_env()

    for query in actual_queries:
        try:
            # "py -3.12" 形式への対応
            if " " in py:
                cmd = py.split() + [str(_SKILL_SCRIPT), query, "--emit=compact", f"--last={last_days}d"]
            else:
                cmd = [py, str(_SKILL_SCRIPT), query, "--emit=compact", f"--last={last_days}d"]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=str(_SKILL_SCRIPT.parent),
                env=env,
            )
            output = result.stdout or ""
            if not output.strip():
                logger.debug("last30days 出力なし (query=%s, stderr=%s)", query, (result.stderr or "")[:200])
                continue

            for kw in _extract_keywords_from_output(output):
                kl = kw.lower()
                if kl not in seen:
                    seen.add(kl)
                    all_keywords.append(kw)
                if len(all_keywords) >= max_keywords:
                    break

        except subprocess.TimeoutExpired:
            logger.warning("last30days タイムアウト (%d 秒, query=%s)", timeout, query)
        except Exception as e:
            logger.warning("last30days 実行エラー (query=%s): %s", query, e)

    logger.info("last30days から %d 件のトレンドキーワードを取得", len(all_keywords))
    return all_keywords[:max_keywords]
