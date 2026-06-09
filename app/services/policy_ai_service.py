"""政策提案AI - Claude CLI を使ったマルチ専門家ディベート。

フロー:
  1. metrics DB から関連データを取得
  2. 人口統計学者・経済学者・社会学者・法学者の視点で順に分析（各 claude 呼び出し）
  3. 総合司会が4施策に統合し JSON で出力
  4. vote_service 経由で DB に保存

実行方法:
  python scripts/generate_policy.py --topic shoushika
  または APScheduler から月2回呼ばれる
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------- Claude CLI ユーティリティ（claude_researcher と同パターン） ----------

def _npm_claude_cmd_paths() -> list:
    out = []
    for key in ("APPDATA", "LOCALAPPDATA"):
        root = os.environ.get(key, "").strip()
        if root:
            p = Path(root) / "npm" / "claude.cmd"
            if p.is_file():
                out.append(p)
    return out


def _find_claude_cmd() -> Optional[str]:
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
        found = shutil.which("claude")
        if found:
            return found
        return None
    for candidate in ("claude", "claude.cmd"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def is_claude_available() -> bool:
    return _find_claude_cmd() is not None


def _subprocess_env() -> dict:
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    return env


def _build_cmd(cmd_path: str) -> list:
    base = [
        "--dangerously-skip-permissions",
        "--allowed-tools", "Write",
        "-p",
        "--input-format", "text",
    ]
    if sys.platform == "win32":
        return ["cmd", "/c", cmd_path] + base
    return [cmd_path] + base


def _call_claude(prompt: str, output_path: Path, timeout: int = 300, label: str = "policy") -> Optional[str]:
    """Claude CLI を呼び出し、JSON 文字列を返す。失敗時 None。"""
    cmd_path = _find_claude_cmd()
    if not cmd_path:
        logger.error("[%s] claude コマンドが見つかりません", label)
        return None
    cmd = _build_cmd(cmd_path)
    try:
        proc = subprocess.run(
            cmd,
            input=prompt.encode("utf-8"),
            capture_output=True,
            timeout=timeout,
            cwd=str(PROJECT_ROOT),
            env=_subprocess_env(),
            shell=False,
        )
        # Windows では Claude CLI が cp932 で出力する場合があるため、複数エンコーディングを試みる
        def _decode(b: bytes) -> str:
            for enc in ("utf-8", "cp932", "latin-1"):
                try:
                    return b.decode(enc)
                except UnicodeDecodeError:
                    continue
            return b.decode("utf-8", errors="replace")
        proc_stdout = _decode(proc.stdout or b"")
        proc_stderr = _decode(proc.stderr or b"")
        if proc.returncode != 0:
            logger.error("[%s] 終了コード %d: %s", label, proc.returncode, proc_stderr[:400])
            return None
        raw = ""
        if output_path.exists():
            raw = output_path.read_text(encoding="utf-8").strip()
        else:
            raw = proc_stdout.strip()
            if "```" in raw:
                raw = "\n".join(l for l in raw.splitlines() if not l.strip().startswith("```")).strip()
            for ch_start, ch_end in (("{", "}"), ("[", "]")):
                i, j = raw.find(ch_start), raw.rfind(ch_end)
                if i != -1 and j != -1 and j > i:
                    raw = raw[i:j + 1].strip()
                    break
        return raw if raw else None
    except subprocess.TimeoutExpired:
        logger.error("[%s] タイムアウト (%d 秒)", label, timeout)
        return None
    except Exception as e:
        logger.error("[%s] 予期せぬエラー: %s", label, e)
        return None


# ---------- メトリクスデータ取得 ----------

def _get_metrics_summary(topic_key: str) -> str:
    """metrics DB から関連データをテキスト化して返す（プロンプト注入用）。"""
    try:
        from app.services.neon_store import use_neon
        if use_neon():
            from app.services.neon_store import neon_metrics_query
            cats = TOPIC_METRICS_CATEGORIES.get(topic_key, [])
            rows = []
            for cat in cats:
                rows += neon_metrics_query(category=cat, limit=30)
        else:
            rows = _sqlite_metrics_summary(topic_key)
        if not rows:
            return "(利用可能な統計データなし)"
        lines = []
        for r in rows[:60]:
            yr = r.get("year") or ""
            mo = r.get("month") or ""
            period = f"{yr}年{mo}月" if mo else (f"{yr}年" if yr else "")
            val = r.get("value")
            unit = r.get("unit") or ""
            name = r.get("name") or ""
            cat = r.get("category") or ""
            src = r.get("source") or ""
            lines.append(f"- [{cat}] {name}: {val} {unit} ({period}) [{src}]")
        return "\n".join(lines)
    except Exception as e:
        logger.warning("_get_metrics_summary 失敗: %s", e)
        return "(統計データ取得失敗)"


def _sqlite_metrics_summary(topic_key: str) -> list:
    try:
        import sqlite3
        db = PROJECT_ROOT / "data" / "metrics.db"
        if not db.exists():
            return []
        conn = sqlite3.connect(str(db))
        cats = TOPIC_METRICS_CATEGORIES.get(topic_key, [])
        rows = []
        for cat in cats:
            rs = conn.execute(
                "SELECT category, name, value, unit, year, month, source FROM metrics "
                "WHERE category = ? ORDER BY year DESC LIMIT 30", (cat,)
            ).fetchall()
            for r in rs:
                rows.append({"category": r[0], "name": r[1], "value": r[2], "unit": r[3], "year": r[4], "month": r[5], "source": r[6]})
        conn.close()
        return rows
    except Exception:
        return []


# トピックごとに参照するメトリクスカテゴリ
TOPIC_METRICS_CATEGORIES = {
    "shoushika": ["人口", "出生", "婚姻", "労働", "財政", "税収", "医療", "教育", "外国人"],
    "roudou":    ["労働", "財政", "税収", "人口"],
    "kyouiku":   ["教育", "人口", "財政", "税収"],
    "energy":    ["エネルギー", "財政", "税収"],
}


# ---------- プロンプト定義 ----------

TOPIC_CONFIGS = {
    "shoushika": {
        "title": "少子化対策",
        "description": "日本の少子化問題に対する効果的な政策提案",
        "context": "日本の合計特殊出生率は2023年に1.20と過去最低を更新した。2070年には総人口が約8700万人に減少する見込み。",
        "keywords": ["出生率", "育児支援", "男性育休", "子育て費用", "婚姻率", "移民政策", "経済的支援"],
    },
}


def _build_expert_prompt(
    expert_role: str,
    topic_config: dict,
    metrics_text: str,
    prev_analysis: str = "",
) -> str:
    topic_title = topic_config["title"]
    context = topic_config["context"]
    prev = f"\n\n【前の専門家の分析】\n{prev_analysis}" if prev_analysis else ""
    return f"""\
あなたは{expert_role}として、日本の「{topic_title}」について専門的見地から分析してください。

【背景・現状】
{context}

【利用可能な統計データ】
{metrics_text}{prev}

以下の観点で日本語で簡潔に分析してください（箇条書き可）:
1. 現状の主要な問題点（2〜3点）
2. あなたの専門分野から見た重要な介入ポイント
3. 提案できる具体的な施策のアイデア（2〜3点）

分析は800文字以内で。"""


def _build_synthesis_prompt(
    topic_config: dict,
    analyses: dict,
    metrics_text: str,
    output_path: str,
) -> str:
    topic_title = topic_config["title"]
    context = topic_config["context"]
    analyses_text = "\n\n".join(
        f"=== {role}の分析 ===\n{text}" for role, text in analyses.items()
    )
    return f"""\
あなたは政策シンクタンクの総合アナリストです。
複数の専門家が日本の「{topic_title}」について分析しました。
これらを統合して、実現可能性が高く効果的な政策提案を4つ選定してください。

【背景】
{context}

【専門家分析】
{analyses_text}

【統計データ】
{metrics_text}

以下のJSON形式で4つの施策を生成し、{output_path} に書き込んでください:

```json
[
  {{
    "rank": 1,
    "title": "施策タイトル（30文字以内）",
    "summary": "施策の要点（150文字以内）",
    "cost_estimate": "年間X兆円（GDP比Y%相当）/ 財源: ○○税・国債等",
    "effect_prediction": "2035年までの効果予測（数値目標含む）",
    "pros": ["メリット1", "メリット2", "メリット3"],
    "cons": ["リスク・デメリット1", "リスク・デメリット2"],
    "expert_sources": ["e-Stat 人口動態調査", "財務省 国債残高", "厚生労働省 人口動態統計"]
  }},
  ...
]
```

4つの施策はそれぞれ異なるアプローチ（経済的支援/制度改革/社会変革/技術・イノベーション等）を取ること。
必ずJSON配列のみを出力し、説明文は含めないこと。"""


# ---------- メイン生成関数 ----------

def _format_expert_analyses(analyses: dict[str, str]) -> list[dict]:
    icons = ["📊", "💹", "👥", "⚖️"]
    result = []
    for i, (role, text) in enumerate(analyses.items()):
        short = role.split("（")[0] if "（" in role else role
        result.append({
            "role": role,
            "short_role": short,
            "icon": icons[i % len(icons)],
            "text": text.strip(),
        })
    return result


def generate_policy_proposals(topic_key: str = "shoushika", timeout_per_call: int = 300) -> Optional[dict]:
    """指定トピックの政策提案を Claude CLI で生成して返す。

    Returns:
        {"proposals": [...], "expert_analyses": [...]} or None on failure
    """
    topic_config = TOPIC_CONFIGS.get(topic_key)
    if not topic_config:
        logger.error("未知のトピック: %s", topic_key)
        return None

    if not is_claude_available():
        logger.warning("Claude CLI が見つかりません。generate_policy_proposals をスキップします。")
        return None

    logger.info("[policy] トピック '%s' の政策提案生成を開始", topic_key)
    metrics_text = _get_metrics_summary(topic_key)
    logger.info("[policy] 統計データ: %d 文字", len(metrics_text))

    experts = [
        "人口統計学者（出生動態・婚姻率・人口推移の専門家）",
        "経済学者（財政政策・労働市場・コスト便益分析の専門家）",
        "社会学者（家族制度・ジェンダー・社会変容の研究者）",
        "法学者（法制度・行政改革・国際比較政策の専門家）",
    ]
    analyses: dict[str, str] = {}

    with tempfile.TemporaryDirectory(prefix="policy_ai_") as tmpdir:
        tmpdir_path = Path(tmpdir)

        for i, expert in enumerate(experts):
            label = f"policy_expert_{i+1}"
            prev_text = "\n".join(analyses.values())
            prompt = _build_expert_prompt(expert, topic_config, metrics_text, prev_analysis=prev_text)
            out_path = tmpdir_path / f"analysis_{i}.txt"
            logger.info("[policy] 専門家 %d/%d: %s", i + 1, len(experts), expert)
            raw = _call_claude(prompt, out_path, timeout=timeout_per_call, label=label)
            if raw:
                analyses[expert] = raw
                logger.info("[policy] 専門家 %d: %d 文字取得", i + 1, len(raw))
            else:
                logger.warning("[policy] 専門家 %d の分析取得失敗（継続）", i + 1)
            time.sleep(1.5)

        if not analyses:
            logger.error("[policy] 全専門家の分析失敗")
            return None

        # 統合
        out_path = tmpdir_path / "proposals.json"
        synthesis_prompt = _build_synthesis_prompt(
            topic_config, analyses, metrics_text, str(out_path)
        )
        logger.info("[policy] 統合プロンプト実行中...")
        raw = _call_claude(synthesis_prompt, out_path, timeout=timeout_per_call * 2, label="policy_synthesis")
        if not raw:
            logger.error("[policy] 統合生成失敗")
            return None

        try:
            proposals = json.loads(raw)
            if not isinstance(proposals, list):
                logger.error("[policy] JSON がリストではありません: %s", type(proposals))
                return None
            validated = []
            for p in proposals[:4]:
                if not isinstance(p, dict) or not p.get("title"):
                    continue
                validated.append({
                    "rank": int(p.get("rank", len(validated) + 1)),
                    "title": str(p.get("title", ""))[:100],
                    "summary": str(p.get("summary", ""))[:500],
                    "cost_estimate": str(p.get("cost_estimate", "")),
                    "effect_prediction": str(p.get("effect_prediction", "")),
                    "pros": [str(x) for x in (p.get("pros") or [])[:5]],
                    "cons": [str(x) for x in (p.get("cons") or [])[:5]],
                    "expert_sources": [str(x) for x in (p.get("expert_sources") or [])[:5]],
                })
            logger.info("[policy] %d 件の施策を生成しました", len(validated))
            if not validated:
                return None
            return {
                "proposals": validated,
                "expert_analyses": _format_expert_analyses(analyses),
            }
        except json.JSONDecodeError as e:
            logger.error("[policy] JSON パースエラー: %s\nraw=%s", e, raw[:200])
            return None


def run_generate_and_save(topic_key: str = "shoushika") -> bool:
    """生成して DB に保存するまでを実行する（スケジューラから呼ぶ）。"""
    topic_config = TOPIC_CONFIGS.get(topic_key)
    if not topic_config:
        logger.error("未知のトピック: %s", topic_key)
        return False

    result = generate_policy_proposals(topic_key)
    if not result:
        logger.error("[policy] 提案生成に失敗しました")
        return False

    proposals = result["proposals"]
    expert_analyses = result.get("expert_analyses", [])

    try:
        from app.services.vote_service import save_policy_topic, save_policy_proposals
        save_policy_topic(
            topic_key,
            topic_config["title"],
            topic_config.get("description", ""),
            expert_analyses=expert_analyses,
        )
        save_policy_proposals(topic_key, proposals)
        logger.info(
            "[policy] トピック '%s' の提案を DB に保存しました（施策 %d 件・議論 %d 件）",
            topic_key,
            len(proposals),
            len(expert_analyses),
        )
        return True
    except Exception as e:
        logger.error("[policy] DB 保存に失敗: %s", e)
        return False
