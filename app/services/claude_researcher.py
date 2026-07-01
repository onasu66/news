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


# ─────────────────────────────────────────────────────────────
# 時間帯スロット定義
#   slot="morning"   8:30  朝の通勤・情報収集層
#   slot="afternoon" 16:30 夕方の学習・仕事終わり層
#   slot="night"     22:00 夜のゆったり読書・深掘り層
# ─────────────────────────────────────────────────────────────

_SLOT_CONFIGS: dict[str, dict] = {
    "morning": {
        "label": "朝（通勤・情報収集）",
        "reader_context": "通勤中や朝の情報収集タイムに読む層（速報・今日の重要ニュースを求めている）",
        "min_buzz_news": 3,
        "category_guide": (
            "国内 40% / 国際 30% / 政治・社会 20% / テクノロジー 10%\n"
            "朝に確認したい「今日の動き」優先。経済・政治・社会の速報を重点収集。"
        ),
        "seo_hint": (
            "読者が検索しそうなクエリ例: 「○○ 速報」「○○ 今日」「○○ どうなった」「○○ とは」\n"
            "Google ニュース急上昇・X トレンドで日本国内の話題を優先的に拾う。"
        ),
        "trend_queries": [
            "Google トレンド 急上昇 日本 {today}",
            "X Twitter トレンド 日本 朝 {today}",
            "NHK 主要ニュース {today}",
        ],
        "paper_themes": "健康・睡眠・朝の習慣 / AI・医療 / 経済・行動経済学",
    },
    "afternoon": {
        "label": "夕方（学習・テック深掘り）",
        "reader_context": "仕事の合間や帰宅前に学習・調査する層（「○○ とは」「○○ 使い方」「○○ 何」を検索する）",
        "min_buzz_news": 3,
        "category_guide": (
            "テクノロジー 40% / 国際 30% / 国内 30%\n"
            "X・Reddit で話題の新サービス・政策・事件の解説記事を優先。"
        ),
        "seo_hint": (
            "読者が検索しそうなクエリ例: 「○○ とは」「○○ 何」「○○ 使い方」「○○ 仕組み」「○○ 違い」\n"
            "SNSでバズった話題の背景解説・新技術の説明記事を優先する。\n"
            "summary では『なぜ重要か・どう影響するか』を必ず含める。"
        ),
        "trend_queries": [
            "Google トレンド 急上昇 日本 {today}",
            "X Twitter トレンド テクノロジー AI {today}",
            "話題の新技術 新サービス {today}",
        ],
        "paper_themes": "AI・機械学習・LLM / 量子コンピュータ / 宇宙・素粒子 / 環境・エネルギー",
    },
    "night": {
        "label": "夜（ゆったり深掘り・雑学）",
        "reader_context": "夜のリラックスタイムに読む層。バズった話題の深掘りニュース＋研究・論文",
        "min_buzz_news": 3,
        "category_guide": (
            "国内 35% / 国際 35% / 政治・社会 20% / テクノロジー 10%\n"
            "Step 1 で X・Reddit・Googleトレンドに上がっていた話題を news に必ず反映。\n"
            "研究結果の解説は papers 配列へ（news に入れない）。"
        ),
        "seo_hint": (
            "ニュース: 「○○ 速報」「○○ なぜ話題」「○○ 最新」\n"
            "論文: 「○○ 研究」「○○ 効果」「○○ 最新」「○○ 理由」\n"
            "論文はタイトルに研究キーワードを含め、summary に方法・数値・生活への応用を書く。"
        ),
        "trend_queries": [
            "Google トレンド 急上昇 日本 {today}",
            "X Twitter トレンド 日本 {today}",
            "Reddit 話題 日本 {today}",
        ],
        "paper_themes": "健康・長寿・睡眠・メンタル / スポーツ科学・栄養 / 神経科学・認知 / 気候・環境 / 社会科学",
    },
}


def _news_paper_separation_rules() -> str:
    return """\
【ニュースと論文の厳密な分離（必須）】
- **news 配列** = 速報・社会現象・X/Reddit/Googleでバズっている話題の記事。
  - 媒体例: nhk.or.jp / reuters.com / apnews.com / afpbb.com / techcrunch.com / bbc.com / japantimes.co.jp
  - category は 国内|国際|政治・社会|テクノロジー|エンタメ|スポーツ のいずれか（研究・論文 禁止）
- **papers 配列** = 学術論文・査読付き研究。category は必ず「研究・論文」。
  - ソース例: arxiv.org / pubmed / nature.com/articles / sciencedaily.com / biorxiv / medrxiv
- **Step 1 で拾ったバズ話題**（Xトレンド・Reddit upvote・Google急上昇）は **必ず news で記事化**する。papers に入れない。
- 以下の URL は **papers のみ**（news 配列に入れたら不合格）:
  arxiv.org / pubmed / nature.com/articles / sciencedaily.com / biorxiv / medrxiv / eurekalert.org / doi.org
"""


def _build_slot_prompt(
    slot: str,
    today: str,
    n_news: int,
    n_papers: int,
    output_file: str,
    use_last30days: bool = False,
) -> str:
    """時間帯スロットに応じたリサーチプロンプトを生成する。
    use_last30days=True のとき last30days スキルを Step 1 で使って
    Reddit/HN のリアルエンゲージメントデータを取得する。
    """
    cfg = _SLOT_CONFIGS.get(slot) or _SLOT_CONFIGS["morning"]
    min_buzz = min(int(cfg.get("min_buzz_news", 5)), n_news)
    separation = _news_paper_separation_rules()

    if use_last30days:
        # last30days スキル（無料: Reddit/HN/Polymarket/YouTube、XはCookie設定時）
        skill_dir = str(Path.home() / ".claude" / "skills" / "last30days" / "scripts").replace("\\", "/")
        py_cmd = _find_python312_plus() or "python3"
        # Step 1 はバズ話題収集 → Step 2 news に使う（全スロット共通方針）
        l30d_topics = {
            "morning": [
                "X Twitter トレンド 日本 バズ 今日",
                "日本 ニュース 速報 政治 社会",
            ],
            "afternoon": [
                "X Twitter トレンド 日本 テクノロジー AI バズ",
                "話題 新サービス スタートアップ 日本",
            ],
            "night": [
                "X Twitter トレンド 日本 バズ 話題",
                "Reddit Japan trending news 今日",
            ],
        }
        topics = l30d_topics.get(slot, l30d_topics["morning"])
        step1 = f"""\
【Step 1: バズ・トレンド収集（last30days スキル使用・無料）】
**目的: Step 2 の news 選定用キーワードリストを作る（papers には使わない）**
以下を Bash で実行し、Reddit upvote・HN points・（X設定時はX）のエンゲージメント上位トピックを取得:

```bash
export EXCLUDE_SOURCES="tiktok,instagram,threads,pinterest,perplexity"
{py_cmd} "{skill_dir}/last30days.py" "{topics[0]}" --emit=compact --last=3d
```

```bash
export EXCLUDE_SOURCES="tiktok,instagram,threads,pinterest,perplexity"
{py_cmd} "{skill_dir}/last30days.py" "{topics[1]}" --emit=compact --last=3d
```

取得結果から「今バズっている話題・固有名詞・事件名・製品名」を10個以上リストアップする。
**このリストの話題は Step 2 の news で必ず記事化する**（ScienceDaily 等の研究サイトに逃がさない）。
失敗時は WebSearch で代替:
- 「X Twitter トレンド 日本 {today}」「Google トレンド 急上昇 日本 {today}」
- 「site:reddit.com/r/newsokuexp OR site:reddit.com/r/japan top {today}」
"""
    else:
        # フォールバック: WebSearch のみ
        trend_queries = "\n".join(
            f'- 「{q.format(today=today)}」' for q in cfg["trend_queries"]
        )
        step1 = f"""\
【Step 1: バズ・トレンド収集（WebSearch）】
**目的: Step 2 の news 選定用。X・Reddit・Googleで「今話題」のキーワードを10個以上集める**
以下を WebSearch で検索し、急上昇・バズキーワードをリストアップする:
{trend_queries}
- 「X Twitter トレンド 日本 {today}」「Google トレンド 急上昇 日本 {today}」
- Reddit: 「site:reddit.com/r/japan OR site:reddit.com/r/newsokuexp top posts {today}」
- HN: 「site:news.ycombinator.com {today} top」
**このリストの話題は Step 2 news で NHK/Reuters 等のニュース記事として記事化する**
"""

    return f"""\
今日は {today}（日本時間）、{cfg["label"]}の配信です。
Web 検索はこの依頼の中で効率よくまとめて行い、同じトレンド調査を何度も繰り返さないでください。

【対象読者】{cfg["reader_context"]}

「知リポAI」ニュースサイト向けに、ニュース 最大 {n_news} 件・学術論文 最大 {n_papers} 件を選定し、
{output_file} に次の JSON オブジェクトだけを書き込んでください（説明文・Markdown のコードフェンス禁止）。
キー名は必ず半角の "news" と "papers" を使ってください。

**効率最優先（重要）**: 1回の検索で見つかった良質な候補だけを採用してください。
件数を埋めるための追加検索・繰り返し検索は不要です。指定件数に届かなくても構いません
（質の低い記事で件数を水増ししないこと）。今ホットな話題を一度のリサーチでまとめて出してください。

**重要: summary フィールドは不要です。代わりに reason（選定理由）を50〜80字で書いてください。**
記事本文は URL から自動取得するため、要約を書く必要はありません。

{{{{
  "news": [
    {{{{
      "title": "タイトル（日本語・28〜42文字。読者が検索しそうなキーワードを前半に）",
      "url": "実在する記事の URL",
      "reason": "この記事を選んだ理由（50〜80字。例:「Xトレンド1位。AI規制法案の国会審議に関する速報」）",
      "source": "媒体名",
      "category": "テクノロジー|国際|国内|政治・社会|エンタメ|スポーツ のいずれか",
      "published": "YYYY-MM-DDTHH:MM:SS",
      "image_url": null
    }}}}
  ],
  "papers": [
    {{{{
      "title": "タイトルは必ず日本語で（英語論文も日本語訳）。研究対象・結果を含む28〜42文字",
      "url": "実在する論文の URL",
      "reason": "この論文を選んだ理由（50〜80字。例:「arXiv新着。睡眠と認知機能の関係を1万人規模で解析した注目研究」）",
      "source": "媒体名（arXiv / Nature / PubMed など）",
      "category": "研究・論文",
      "published": "YYYY-MM-DDTHH:MM:SS",
      "image_url": null
    }}}}
  ]
}}}}

{step1}

{separation}

【Step 2: ニュース】news に最大 {n_news} 件（届かなくても可・件数埋めの再検索は禁止）
カテゴリ比率の目安: {cfg["category_guide"]}

- **最低 {min_buzz} 件**は Step 1 の X/Reddit/Google バズ話題に紐づく記事にすること
- バズ話題の記事 URL は NHK / Reuters / AP / TechCrunch / BBC / 共同 / 読売 等の**ニュース媒体**
- ScienceDaily・Nature・arXiv 等の研究 URL は news に入れない（Step 3 papers へ）
- 訃報・芸能ゴシップ・選挙速報（単なる結果発表）は除外
- reason は 50〜80字。「なぜ今日これを選んだか」を端的に（バズ理由・重要度・対象読者）
- {cfg["seo_hint"]}

【Step 3: 論文】papers に最大 {n_papers} 件（届かなくても可・件数埋めの再検索は禁止）
- 優先テーマ: {cfg["paper_themes"]}
- 海外の英語論文を重視（papers の過半数を英語論文にする）
- arXiv / PubMed / Nature / Science / bioRxiv / medrxiv / ScienceDaily 等。category は必ず「研究・論文」
- URL は必ず実在する論文（架空禁止）。abstract や本文が読めるページ（PDF直リンクのみは避け abs ページを優先）
- reason は 50〜80字。研究の新規性・なぜ今注目されるかを端的に

【ニュースの使用禁止メディア（ペイウォール）】
- bloomberg.com / wsj.com / ft.com / nytimes.com / economist.com

【ニュースの優先メディア（news 配列のみ）】
- nhk.or.jp / reuters.com / apnews.com / afpbb.com / techcrunch.com / theverge.com / bbc.com / cnn.com / japantimes.co.jp / kyodonews.net / yomiuri.co.jp

【論文の優先ソース（papers 配列のみ）】
- arxiv.org / pubmed.ncbi.nlm.nih.gov / nature.com/articles / science.org / biorxiv.org / medrxiv.org / sciencedaily.com

上記オブジェクトを {output_file} に書き込んで完了してください。
"""


# 旧プロンプト（後方互換のため残す。run_claude_research の slot 未指定時に使用）
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
                env=_subprocess_env(),
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
                out = out_file.read_text(encoding="utf-8-sig").strip()
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
    # npm config get prefix で取得したカスタムプレフィックスを最優先
    _extra_prefixes = [
        Path(r"D:\app\npm-global"),
    ]
    for prefix in _extra_prefixes:
        p = prefix / "claude.cmd"
        if p.is_file():
            out.append(p)
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


def _find_python312_plus() -> str | None:
    """Python 3.12+ 実行ファイルのパスを返す。見つからなければ None。"""
    import re

    def _ver_ok(cmd: str) -> bool:
        try:
            r = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=5)
            m = re.search(r"Python (\d+)\.(\d+)", r.stdout + r.stderr)
            if m and (int(m.group(1)), int(m.group(2))) >= (3, 12):
                return True
        except Exception:
            pass
        return False

    # last30days スキルの専用 venv（uv で作成）を最優先で確認
    skill_venv_py = Path.home() / ".claude" / "skills" / "last30days" / ".venv" / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )
    if skill_venv_py.exists() and _ver_ok(str(skill_venv_py)):
        return str(skill_venv_py)

    # uv 管理の Python 3.12+
    uv_py_base = Path.home() / "AppData" / "Roaming" / "uv" / "python"
    if uv_py_base.exists():
        for d in sorted(uv_py_base.iterdir(), reverse=True):
            p = d / "python.exe" if sys.platform == "win32" else d / "bin" / "python3"
            if p.exists() and _ver_ok(str(p)):
                return str(p)

    # よくある名前を順にチェック
    for name in ("python3.14", "python3.13", "python3.12", "python3"):
        p = shutil.which(name)
        if p and _ver_ok(p):
            return p

    # Windows: py ランチャー経由
    if sys.platform == "win32":
        for ver in ("3.14", "3.13", "3.12"):
            try:
                r = subprocess.run(
                    ["py", f"-{ver}", "--version"],
                    capture_output=True, text=True, timeout=5
                )
                if r.returncode == 0:
                    return f"py -{ver}"
            except Exception:
                pass
        # conda/miniconda の既知パスも確認
        for d in [
            Path.home() / "miniconda3",
            Path.home() / "anaconda3",
            Path("C:/ProgramData/miniconda3"),
            Path("D:/conda"),
        ]:
            p = d / "python.exe"
            if p.exists() and _ver_ok(str(p)):
                return str(p)
    return None


def _is_last30days_available() -> bool:
    """last30days スキルが利用可能かチェック（Python 3.12+ が必要）。"""
    skill_script = Path.home() / ".claude" / "skills" / "last30days" / "scripts" / "last30days.py"
    if not skill_script.exists():
        return False
    return _find_python312_plus() is not None


def _load_env_file_into(env: dict[str, str], path: Path) -> None:
    if not path.is_file():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip("\"'")
            if k and v and not env.get(k):
                env[k] = v
    except Exception:
        pass


def _subprocess_env() -> dict[str, str]:
    """Claude / last30days subprocess 用。X Cookie と EXCLUDE_SOURCES を確実に渡す。"""
    env = os.environ.copy()
    _load_env_file_into(env, PROJECT_ROOT / ".env")
    _load_env_file_into(env, Path.home() / ".config" / "last30days" / ".env")
    if not env.get("EXCLUDE_SOURCES"):
        env["EXCLUDE_SOURCES"] = "tiktok,instagram,threads,pinterest,perplexity"
    return env


def _x_auth_configured() -> bool:
    env = _subprocess_env()
    return bool(env.get("AUTH_TOKEN") and env.get("CT0"))


def _build_cmd(cmd_path: str, use_last30days: bool = False) -> list[str]:
    # last30days スキルは Bash ツール（Python スクリプト実行）が必要
    tools = "WebSearch,Write,Bash" if use_last30days else "WebSearch,Write"
    base = [
        "--dangerously-skip-permissions",
        "--allowed-tools", tools,
        "--max-turns", "12",
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
    label: str, prompt: str, output_file: Path, timeout: int,
    use_last30days: bool = False,
) -> str | None:
    """Claude を1回起動し、output_file または stdout から JSON テキストを返す。失敗時は None。"""
    repair_claude_user_config_if_corrupted()
    cmd_path = _find_claude_cmd()
    if not cmd_path:
        logger.error("[%s] claude コマンドが見つかりません", label)
        return None

    cmd = _build_cmd(cmd_path, use_last30days=use_last30days)
    logger.info("[%s] Claude 起動 (タイムアウト=%d 秒)", label, timeout)

    def _decode_cli_bytes(b: bytes) -> str:
        for enc in ("utf-8", "cp932", "latin-1"):
            try:
                return b.decode(enc)
            except UnicodeDecodeError:
                continue
        return b.decode("utf-8", errors="replace")

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
        proc_stdout = _decode_cli_bytes(proc.stdout or b"")
        proc_stderr = _decode_cli_bytes(proc.stderr or b"")

        if proc.returncode != 0:
            logger.error(
                "[%s] 終了コード %d:\nstdout=%s\nstderr=%s",
                label,
                proc.returncode,
                proc_stdout[:500],
                proc_stderr[:500],
            )
            return None

        raw = ""
        if output_file.exists():
            raw = output_file.read_text(encoding="utf-8-sig").strip()
        else:
            stdout = proc_stdout.strip()
            stderr_tail = proc_stderr[-800:]
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


def _detect_current_slot() -> str:
    """現在時刻からスロット名を返す。article_seed_from_curated など外部から参照可能。"""
    hour = datetime.now(JST).hour
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 19:
        return "afternoon"
    return "night"


def _build_curation_prompt(
    news_candidates: list[dict],
    paper_candidates: list[dict],
    slot: str,
    today: str,
    n_news: int,
    n_papers: int,
    output_file: str,
) -> str:
    """事前収集済みの記事・論文リストから Claude に選定させるプロンプト。WebSearch 不要。"""
    cfg = _SLOT_CONFIGS.get(slot) or _SLOT_CONFIGS["morning"]
    separation = _news_paper_separation_rules()

    news_json = json.dumps(
        [{"idx": i, "title": a["title"], "url": a["url"],
          "source": a.get("source", ""), "category": a.get("category", ""),
          "published": a.get("published", ""), "keyword": a.get("keyword", "")}
         for i, a in enumerate(news_candidates)],
        ensure_ascii=False,
    )
    paper_json = json.dumps(
        [{"idx": i, "title": p["title"], "url": p["url"],
          "source": p.get("source", ""), "published": p.get("published", ""),
          "summary": p.get("summary", "")[:150]}
         for i, p in enumerate(paper_candidates)],
        ensure_ascii=False,
    )

    return f"""\
今日は {today}（日本時間）、{cfg["label"]}の配信です。
【対象読者】{cfg["reader_context"]}

「知リポAI」向けに、下記の事前収集済みリストから最良の記事・論文を選定してください。
Web検索は不要です。リスト内の情報だけで判断してください。

カテゴリ比率の目安: {cfg["category_guide"]}

{separation}

【ニュース候補リスト（{len(news_candidates)} 件）】
```json
{news_json}
```

【論文候補リスト（{len(paper_candidates)} 件）】
```json
{paper_json}
```

上記リストから選び、{output_file} に以下の JSON オブジェクトだけを書き込んでください（説明文・コードフェンス禁止）。

{{{{
  "news": [
    {{{{
      "title": "タイトル（日本語・28〜42文字。英語タイトルは日本語に意訳）",
      "url": "候補リストにある実在 URL をそのまま使用",
      "reason": "選定理由（50〜80字。バズ理由・重要度・対象読者）",
      "source": "候補リストの source をそのまま",
      "category": "テクノロジー|国際|国内|政治・社会|エンタメ|スポーツ のいずれか",
      "published": "候補リストの published をそのまま",
      "image_url": null
    }}}}
  ],
  "papers": [
    {{{{
      "title": "タイトル（必ず日本語・英語論文も日本語訳）",
      "url": "候補リストにある実在 URL をそのまま使用",
      "reason": "選定理由（50〜80字。研究の新規性・なぜ今注目か）",
      "source": "候補リストの source をそのまま",
      "category": "研究・論文",
      "published": "候補リストの published をそのまま",
      "image_url": null
    }}}}
  ]
}}}}

選定基準:
- news: ニュース候補から最大 {n_news} 件。keyword フィールドがある場合はバズ話題として優先。
- papers: 論文候補から最大 {n_papers} 件。AI・健康・宇宙・心理学など多様なテーマを選ぶ。
- 重複 URL 禁止・候補リストにない URL を作らない・英語タイトルは必ず日本語に意訳。
- 同一 keyword の記事が複数ある場合は最も情報量が多い1件だけを選ぶ（同じ話題を複数選ばない）。
  ※話題まとめ記事は別途自動生成済みのため、個別URLの重複選定は不要。
"""


def _build_cmd_write_only(cmd_path: str) -> list[str]:
    """選定専用: Write ツールのみ（WebSearch・Bash 不要）"""
    base = [
        "--dangerously-skip-permissions",
        "--allowed-tools", "Write",
        "--max-turns", "5",
        "-p",
        "--input-format", "text",
    ]
    if sys.platform == "win32":
        return ["cmd", "/c", cmd_path] + base
    return [cmd_path] + base


def _build_llm_curation_prompt(
    news_candidates: list[dict],
    paper_candidates: list[dict],
    slot: str,
    today: str,
    n_news: int,
    n_papers: int,
) -> str:
    cfg = _SLOT_CONFIGS.get(slot) or _SLOT_CONFIGS["morning"]
    separation = _news_paper_separation_rules()
    news_json = json.dumps(
        [{"idx": i, "title": a["title"], "url": a["url"],
          "source": a.get("source", ""), "category": a.get("category", ""),
          "published": a.get("published", ""), "keyword": a.get("keyword", "")}
         for i, a in enumerate(news_candidates)],
        ensure_ascii=False,
    )
    paper_json = json.dumps(
        [{"idx": i, "title": p["title"], "url": p["url"],
          "source": p.get("source", ""), "published": p.get("published", ""),
          "summary": p.get("summary", "")[:100]}
         for i, p in enumerate(paper_candidates)],
        ensure_ascii=False,
    )
    return f"""今日は {today}（日本時間）、{cfg["label"]}の配信です。
【対象読者】{cfg["reader_context"]}

「知リポAI」向けに、下記のリストから最良の記事・論文を選定し、JSONのみ出力してください。
Web検索不要。説明文・コードフェンス・前置き禁止。

カテゴリ比率: {cfg["category_guide"]}
{separation}

【ニュース候補（{len(news_candidates)}件）】
{news_json}

【論文候補（{len(paper_candidates)}件）】
{paper_json}

{{
  "news": [
    {{"title":"日本語28〜42文字","url":"候補のURLそのまま","reason":"選定理由50〜80字","source":"候補のsourceそのまま","category":"テクノロジー|国際|国内|政治・社会|エンタメ|スポーツ","published":"候補のpublishedそのまま","image_url":null}}
  ],
  "papers": [
    {{"title":"必ず日本語","url":"候補のURLそのまま","reason":"選定理由50〜80字","source":"候補のsourceそのまま","category":"研究・論文","published":"候補のpublishedそのまま","image_url":null}}
  ]
}}

news最大{n_news}件（keyword優先）、papers最大{n_papers}件。重複URL禁止。英語タイトルは必ず日本語に意訳。
同一keywordの記事が複数ある場合は最も情報量が多い1件のみ選ぶ（話題まとめ記事は別途自動生成済み）。"""


def _extract_json(raw: str) -> str | None:
    """レスポンス文字列から JSON オブジェクトを取り出す。"""
    if not raw:
        return None
    if "```" in raw:
        raw = "\n".join(ln for ln in raw.splitlines() if not ln.strip().startswith("```")).strip()
    if raw.startswith("{"):
        return raw
    i, j = raw.find("{"), raw.rfind("}")
    if i != -1 and j > i:
        return raw[i:j + 1].strip()
    return None


def _curate_with_llm(
    news_candidates: list[dict],
    paper_candidates: list[dict],
    slot: str,
    today: str,
    n_news: int,
    n_papers: int,
) -> str | None:
    """Gemini/OpenAI で候補から最良の記事・論文を選定し JSON 文字列を返す。
    候補を絞り込んでから送信し、JSON 不正時は候補を半減してリトライする。"""
    from app.utils.llm_client import GeminiClient

    # 候補を絞る（入力が大きすぎると Gemini がレスポンスを途中で切る）
    MAX_NEWS = 25
    MAX_PAPERS = 15
    news_subset = news_candidates[:MAX_NEWS]
    paper_subset = paper_candidates[:MAX_PAPERS]

    for attempt, (nc, pc) in enumerate([
        (news_subset, paper_subset),
        (news_subset[:12], paper_subset[:8]),  # リトライ時はさらに絞る
    ]):
        try:
            prompt = _build_llm_curation_prompt(nc, pc, slot, today, n_news, n_papers)
            client = GeminiClient()
            resp = client.chat.completions.create(
                model="gemini-2.5-flash",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=8192,
                temperature=0.3,
                gemini_task="curation",
                response_format={"type": "json_object"},
            )
            raw = (resp.choices[0].message.content or "").strip()
            if not raw:
                logger.warning("Gemini curation: 空レスポンス (attempt=%d)", attempt + 1)
                continue
            extracted = _extract_json(raw)
            if not extracted:
                logger.warning("Gemini curation: JSON 抽出失敗 (attempt=%d, raw=%r)", attempt + 1, raw[:200])
                continue
            # パース検証
            try:
                json.loads(extracted)
                return extracted
            except json.JSONDecodeError as e:
                logger.warning("Gemini curation: JSON 不正 (attempt=%d): %s | raw=%r", attempt + 1, e, raw[:300])
        except Exception as e:
            logger.warning("Gemini curation 失敗 (attempt=%d): %s", attempt + 1, e)

    return None


def run_claude_research_v2(
    n_news: int = 8,
    n_papers: int = 7,
    timeout: int = 600,
    slot: str | None = None,
) -> bool:
    """
    【新方式】Python 側で記事・論文を事前収集 → Claude は選定のみ行う。

    処理フロー:
      1. last30days.py (直接実行) → トレンドキーワード
      2. Google Trends → 急上昇キーワード追加
      3. Google News RSS → キーワード別ニュース候補収集
      4. 論文 RSS フィード → 論文候補収集
      5. Claude (Write のみ・max-turns 5) → 最良を選んで JSON 出力

    戻り値: curated_articles.json に 1 件以上保存できれば True。
    失敗時は False を返し、呼び出し元で run_claude_research() にフォールバックさせること。
    """
    if slot is None:
        slot = _detect_current_slot()

    today = datetime.now(JST).strftime("%Y-%m-%d")
    slot_label = (_SLOT_CONFIGS.get(slot) or {}).get("label", slot)
    logger.info("Claude 選定 v2 開始: slot=%s (%s) news=%d papers=%d", slot, slot_label, n_news, n_papers)

    # ── Step 1: トレンドキーワード収集 ─────────────────────────────────────
    keywords: list[str] = []

    # last30days (X/Reddit/HN エンゲージメント)
    try:
        from app.services.last30days_bridge import fetch_trending_keywords, is_available as l30d_ok
        if l30d_ok():
            slot_queries = {
                "morning": ["日本 ニュース 話題 今日", "国内 速報 今日"],
                "afternoon": ["テクノロジー AI 話題 最新", "日本 ニュース 話題"],
                "night": ["日本 話題 バズ 今日", "Reddit Japan trending"],
            }
            kws = fetch_trending_keywords(
                queries=slot_queries.get(slot, slot_queries["morning"]),
                last_days=2,
                max_keywords=10,
                timeout=90,
            )
            keywords.extend(kws)
            logger.info("last30days キーワード: %d件", len(kws))
    except Exception as e:
        logger.warning("last30days_bridge 失敗: %s", e)

    # Google Trends (公式 RSS)
    try:
        from app.services.trends_service import fetch_google_trends
        for item in fetch_google_trends()[:10]:
            kw = item.keyword.strip()
            if kw and kw not in keywords:
                keywords.append(kw)
    except Exception as e:
        logger.warning("Google Trends 取得失敗: %s", e)

    if not keywords:
        logger.warning("トレンドキーワードが 0 件。v2 をスキップしてフォールバックします")
        return False

    logger.info("トレンドキーワード合計: %d件 → %s", len(keywords), keywords[:8])

    # ── Step 2: ニュース候補収集（Google News RSS）──────────────────────────
    news_candidates: list[dict] = []
    try:
        from app.services.google_news_rss import fetch_news_for_keywords
        news_candidates = fetch_news_for_keywords(
            keywords,
            max_per_keyword=6,
            max_total=60,
        )
        logger.info("ニュース候補: %d件", len(news_candidates))
    except Exception as e:
        logger.warning("Google News RSS 取得失敗: %s", e)

    if not news_candidates:
        logger.warning("ニュース候補が 0 件。v2 をスキップしてフォールバックします")
        return False

    # ── Step 2.5: 話題まとめ記事生成（同一トレンドキーワードに複数記事 → 1本にまとめる）
    try:
        from app.services.topic_digest import run_topic_digest
        digest_count = run_topic_digest(news_candidates, max_topics=3)
        if digest_count:
            logger.info("話題まとめ記事 %d 本生成完了", digest_count)
    except Exception as e:
        logger.warning("話題まとめ記事生成をスキップ: %s", e)

    # ── Step 3: 論文候補収集（既存 RSS フィード）────────────────────────────
    paper_candidates: list[dict] = []
    try:
        from app.services.paper_rss_fetcher import fetch_paper_candidates
        paper_candidates = fetch_paper_candidates(max_per_feed=5, max_total=40)
        logger.info("論文候補: %d件", len(paper_candidates))
    except Exception as e:
        logger.warning("論文 RSS 取得失敗: %s", e)

    # ── Step 4: 選定（Claude CLI 優先 → Gemini フォールバック）────────────
    started = time.perf_counter()
    raw: str | None = None

    repair_claude_user_config_if_corrupted()
    cmd_path = _find_claude_cmd()
    if cmd_path:
        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = Path(tmpdir) / "curated_batch.json"
            curation_prompt = _build_curation_prompt(
                news_candidates, paper_candidates,
                slot, today, n_news, n_papers,
                str(out_file).replace("\\", "/"),
            )
            cmd = _build_cmd_write_only(cmd_path)
            logger.info("Claude 選定 v2: Write のみ・max-turns 5 で起動 (news=%d件候補, papers=%d件候補)", len(news_candidates), len(paper_candidates))

            cl_started = time.perf_counter()
            try:
                proc = subprocess.run(
                    cmd,
                    input=curation_prompt.encode("utf-8"),
                    capture_output=True,
                    timeout=timeout,
                    cwd=str(PROJECT_ROOT),
                    env=_subprocess_env(),
                    shell=False,
                )
            except subprocess.TimeoutExpired:
                logger.error("Claude 選定 v2 タイムアウト (%d 秒)", timeout)
                proc = None
            except Exception as e:
                logger.error("Claude 選定 v2 起動エラー: %s", e)
                proc = None

            if proc is not None:
                cl_elapsed = time.perf_counter() - cl_started

                def _decode(b: bytes) -> str:
                    for enc in ("utf-8", "cp932", "latin-1"):
                        try:
                            return b.decode(enc)
                        except UnicodeDecodeError:
                            continue
                    return b.decode("utf-8", errors="replace")

                stdout = _decode(proc.stdout or b"")
                stderr = _decode(proc.stderr or b"")

                if proc.returncode != 0:
                    logger.warning(
                        "Claude 選定 v2 失敗 code=%d stdout=%s stderr=%s",
                        proc.returncode, stdout[:300], stderr[:300],
                    )
                else:
                    claude_raw = ""
                    if out_file.exists():
                        claude_raw = out_file.read_text(encoding="utf-8-sig").strip()
                    if not claude_raw:
                        claude_raw = stdout.strip()
                        if "```" in claude_raw:
                            claude_raw = "\n".join(ln for ln in claude_raw.splitlines() if not ln.strip().startswith("```")).strip()
                        if claude_raw:
                            s = claude_raw.lstrip()
                            if not s.startswith("{"):
                                i, j = claude_raw.find("{"), claude_raw.rfind("}")
                                if i != -1 and j > i:
                                    claude_raw = claude_raw[i:j + 1].strip()
                    if claude_raw:
                        raw = claude_raw
                        elapsed = cl_elapsed
                        _record_usage("curation_v2", prompt="[claude]", output=raw, elapsed_sec=cl_elapsed, ok=True)
                        logger.info("Claude 選定成功 (%.1f 秒)", cl_elapsed)
                    else:
                        logger.warning("Claude 選定 v2: 出力が空。Gemini にフォールバック")
    else:
        logger.info("Claude CLI 未インストール。Gemini で選定")

    if not raw:
        logger.info("Gemini で選定中 (news=%d件候補, papers=%d件候補)...", len(news_candidates), len(paper_candidates))
        raw = _curate_with_llm(news_candidates, paper_candidates, slot, today, n_news, n_papers)
        elapsed = time.perf_counter() - started
        if raw:
            logger.info("Gemini 選定成功 (%.1f 秒)", elapsed)
            _record_usage("curation_v2_gemini", prompt="[gemini]", output=raw, elapsed_sec=elapsed, ok=True)
        else:
            logger.error("Claude・Gemini ともに選定失敗。v2 中断")
            return False

    try:
        all_articles, n_news_ok, n_papers_ok = _parse_curated_research_json(raw)
    except (json.JSONDecodeError, ValueError) as e:
        logger.error("Claude 選定 v2 JSON パース失敗: %s", e)
        return False

    if not all_articles:
        logger.error("Claude 選定 v2: ニュース・論文ともに 0 件")
        return False

    try:
        from app.services.google_news_url import resolve_google_news_url
        from app.services.paywall_domains import is_paywalled_url

        filtered: list[dict] = []
        for art in all_articles:
            u = (art.get("url") or art.get("link") or "").strip()
            if u:
                art["url"] = resolve_google_news_url(u)
            cat = (art.get("category") or "").strip()
            is_paper = cat == "研究・論文" or "arxiv.org" in (art.get("url") or "")
            if not is_paper and u and is_paywalled_url(art.get("url") or u):
                logger.info("選定結果から有料メディアを除外: %s", (art.get("title") or "")[:50])
                continue
            filtered.append(art)
        all_articles = filtered
    except Exception as e:
        logger.warning("選定結果の URL 解決をスキップ: %s", e)

    CURATED_FILE.write_text(
        json.dumps(all_articles, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(
        "Claude 選定 v2 完了: 合計 %d 件 (ニュース %d / 論文 %d) 保存 (%.1f 秒)",
        len(all_articles), n_news_ok, n_papers_ok, elapsed,
    )
    return True


def run_claude_research(
    n: int = 15,
    n_news: int = 8,
    n_papers: int = 7,
    timeout: int = 900,
    slot: str | None = None,
) -> bool:
    """
    Claude を1回だけ起動し、ニュースと論文をまとめてリサーチして curated_articles.json を更新する。

    slot: "morning" / "afternoon" / "night" を指定すると時間帯最適化プロンプトを使用。
          None の場合は現在時刻から自動判定。
    n は呼び出し互換のため残す（n_news + n_papers と揃える想定）。戻り値: 1件以上取得できれば True。
    """
    _ = n

    # slot 未指定なら現在時刻から自動判定
    if slot is None:
        hour = datetime.now(JST).hour
        if 5 <= hour < 12:
            slot = "morning"
        elif 12 <= hour < 19:
            slot = "afternoon"
        else:
            slot = "night"

    today = datetime.now(JST).strftime("%Y-%m-%d")
    slot_label = (_SLOT_CONFIGS.get(slot) or {}).get("label", slot)

    # last30days スキルが使えるか確認（Python 3.12+ 必須・無料ソースのみ利用）
    use_l30d = _is_last30days_available()
    logger.info(
        "Claude リサーチ開始: スロット=%s (%s) ニュース=%d 論文=%d last30days=%s X=%s",
        slot, slot_label, n_news, n_papers,
        "有効" if use_l30d else "無効(Python3.12未インストール)",
        "有効" if _x_auth_configured() else "未設定(scripts/setup_x_cookies.py)",
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        out_file = Path(tmpdir) / "curated_batch.json"
        prompt = _build_slot_prompt(
            slot, today, n_news, n_papers,
            str(out_file).replace("\\", "/"),
            use_last30days=use_l30d,
        )
        raw = _invoke_claude_research_session(
            f"ニュース+論文[{slot}]", prompt, out_file, timeout,
            use_last30days=use_l30d,
        )
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
