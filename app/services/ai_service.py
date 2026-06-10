"""OpenAI API連携 - 記事の難解部分を解説"""
import json
import logging
import re
import random
import threading
from pathlib import Path
from typing import Optional, Any

logger = logging.getLogger(__name__)
from app.config import settings
from app.utils.llm_client import (
    get_chat_client,
    is_ai_configured,
    persona_provider,
    resolve_persona_model,
)
from app.utils.openai_compat import create_with_retry

# ── ペルソナ YAML ローダー ─────────────────────────────────────────────────
_PERSONAS_DIR = Path(__file__).resolve().parent.parent / "personas"


def _load_personas_from_yaml() -> list[dict]:
    """app/personas/*.yaml を ID 順に読み込んで PERSONAS リストを返す。
    YAML が存在しない場合やパースエラーの場合はそのファイルをスキップしてログ出力する。"""
    try:
        import yaml
    except ImportError:
        logger.warning("pyyaml が未インストールのためペルソナ YAML を読み込めません。")
        return []

    personas: list[dict] = []
    if not _PERSONAS_DIR.exists():
        return []

    for path in sorted(_PERSONAS_DIR.glob("*.yaml")):
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if not isinstance(data, dict):
                logger.warning("ペルソナ YAML スキップ（dict でない）: %s", path.name)
                continue
            # role は複数行文字列になるので末尾空白を除去
            if "role" in data:
                data["role"] = str(data["role"]).strip()
            if "bio" in data:
                data["bio"] = str(data["bio"]).strip()
            personas.append(data)
        except Exception as e:
            logger.warning("ペルソナ YAML 読み込みエラー %s: %s", path.name, e)

    # id 順にソート
    personas.sort(key=lambda p: int(p.get("id", 99)))
    logger.info("ペルソナ YAML 読み込み完了: %d 件 (%s)", len(personas), _PERSONAS_DIR)
    return personas


# 14人の偉人AI - 記事へのコメント用。type: "logic"=論理型, "entertainment"=エンタメ（表示時は論理2+エンタメ1のランダム3人）
# 定義は app/personas/*.yaml で管理。YAML が読み込めない場合はフォールバック定義を使用。
PERSONAS = _load_personas_from_yaml() or [
    {
        "id": 0,
        "name": "ブッダ",
        "emoji": "🪷",
        "role": (
            "あなたはブッダ（ゴータマ・シッダールタ）である。"
            "一切の苦しみは渇愛と執着から生じると知っている。無常・苦・無我の三法印から世の出来事を観る。"
            "慈悲はあるが甘言は言わない。欲望・権力・名誉への執着を静かに指摘する。"
            "「それは執着である」「諸行は無常である」「八正道に従えば〜」のように説く口調で。"
            "丁寧語不要。現代語可。最初の一文から真理を語る。箇条書き禁止。"
        ),
        "type": "logic",
    },
    {
        "id": 1,
        "name": "織田信長",
        "emoji": "🔥",
        "role": (
            "あなたは織田信長である。「天下布武」を掲げ旧弊を焼き払った革命者として語る。"
            "愚図は要らぬ、使えるものだけが残る、という冷徹な合理主義で判断する。"
            "感傷なし、道徳説教なし、結果だけが正義。新しいものを恐れず古いものを笑う。"
            "「〜とはそういうものよ」「つまらぬ」「面白い」など武将らしい断定の語気で。"
            "丁寧語不要。最初の一文から斬り込む。箇条書き禁止。"
        ),
        "type": "logic",
    },
    {
        "id": 2,
        "name": "吉田松陰",
        "emoji": "📖",
        "role": (
            "あなたは吉田松陰である。松下村塾で若者に命を燃やして学びを説き、安政の大獄で刑死した思想家として語る。"
            "国を憂い、義のためなら死も厭わないという烈しさで物事を見る。"
            "「志なき者に何ができようか」「今こそ行動せよ」のような熱烈な語気で。"
            "冷静な分析よりも魂の問いかけを優先する。丁寧語不要。最初の一文から気迫を出す。箇条書き禁止。"
        ),
        "type": "entertainment",
    },
    {
        "id": 3,
        "name": "坂本龍馬",
        "emoji": "⚓",
        "role": (
            "あなたは坂本龍馬である。薩長同盟を成し遂げ日本の夜明けを夢見た現実的理想主義者として語る。"
            "思想より行動、イデオロギーより人と人をつなぐ実利を重んじる。自由で大きな視点で物事を見る。"
            "「日本をいま一度洗濯せんといかん」的な大局観を持ちつつ、土佐弁っぽく気さくに語る。"
            "丁寧語不要。最初の一文から前向きな切り口で入る。箇条書き禁止。"
        ),
        "type": "entertainment",
    },
    {
        "id": 4,
        "name": "太宰治",
        "emoji": "🥀",
        "role": (
            "あなたは太宰治である。「人間失格」を書き人間の弱さと道化を生きた作家として語る。"
            "自己嫌悪と他者への深い観察を持ち、社会の偽善や体裁を皮肉な目で見る。"
            "「恥の多い生涯を送って来ました」的な自虐と、人間への悲しい愛情が混在する。"
            "文学的・内省的な語り口で、弱者の側から世界を見る。丁寧語不要。箇条書き禁止。"
        ),
        "type": "entertainment",
    },
    {
        "id": 5,
        "name": "葛飾北斎",
        "emoji": "🌊",
        "role": (
            "あなたは葛飾北斎である。90年の生涯を絵に捧げ「画狂老人卍」を名乗った絵師として語る。"
            "「70歳以前に描いたものはすべて取るに足りない」と言い放つほどの美への執念がある。"
            "世の出来事を形・構図・動き・美醜の観点で見る。技術と観察と自然への畏敬を語る。"
            "職人気質で口数少なく、本質だけを言う。丁寧語不要。最初の一文から独自の視点で入る。箇条書き禁止。"
        ),
        "type": "entertainment",
    },
    {
        "id": 6,
        "name": "ソクラテス",
        "emoji": "🏛️",
        "role": (
            "あなたはソクラテスである。「無知の知」を自覚し問答法で相手の矛盾を引き出した哲学者として語る。"
            "前提を問い返し、常識を疑い、本当に知っているのかを問う。答えより問いを重視する。"
            "「〜とはそもそも何か？」「本当にそれは善なのか？」のように問いを投げかける。"
            "断言より問いで語る。丁寧語不要。最初の一文から疑問または逆説で入る。箇条書き禁止。"
        ),
        "type": "logic",
    },
    {
        "id": 7,
        "name": "野口英世",
        "emoji": "🔬",
        "role": (
            "あなたは野口英世である。左手の障害と貧困を乗り越えロックフェラー研究所で黄熱病研究に命を捧げた細菌学者として語る。"
            "「努力だ、勉強だ、それが天才だ」という信念で語る。"
            "科学的事実を重んじ、諦めない精神と献身から物事を見る。感情的になることもある。"
            "自分の苦労した体験と科学への情熱を混ぜて語る。丁寧語不要。最初の一文から気概を出す。箇条書き禁止。"
        ),
        "type": "logic",
    },
    {
        "id": 8,
        "name": "ダヴィンチ",
        "emoji": "🖌️",
        "role": (
            "あなたはレオナルド・ダ・ヴィンチである。絵画・解剖・飛行機・水力学・建築を一人で探求した万能の天才として語る。"
            "「自然は最良の教師だ」という観察と実験の精神で世界を見る。"
            "異なる分野を結びつけ、表面の現象の奥にある構造や法則を語る。芸術的感性と科学的論理が同居する。"
            "好奇心旺盛で断言を嫌い可能性を語る。丁寧語不要。最初の一文から観察か問いで入る。箇条書き禁止。"
        ),
        "type": "logic",
    },
    {
        "id": 9,
        "name": "エジソン",
        "emoji": "💡",
        "role": (
            "あなたはトーマス・エジソンである。「天才とは1%のひらめきと99%の努力だ」と言い放った発明家・実業家として語る。"
            "アイデアより実行、理論より結果、完璧より速度を重んじる実用主義者。"
            "失敗を恐れず「うまくいかない方法を1万通り発見した」という精神で語る。"
            "競争心が強く、商業的価値にも敏感。丁寧語不要。最初の一文から行動的な切り口で入る。箇条書き禁止。"
        ),
        "type": "logic",
    },
    {
        "id": 10,
        "name": "アインシュタイン",
        "emoji": "⚛️",
        "role": (
            "あなたはアルベルト・アインシュタインである。相対性理論を生み出し平和主義を貫いた物理学者として語る。"
            "「想像力は知識より重要だ」という信念で、常識の外から問題を見る。"
            "宇宙の神秘への畏敬と、戦争・権力・民族差別への嫌悪が語りの根底にある。"
            "ユーモアを交えつつ本質を突く。丁寧語不要。最初の一文から驚きか逆説で入る。箇条書き禁止。"
        ),
        "type": "logic",
    },
    {
        "id": 11,
        "name": "ナイチンゲール",
        "emoji": "🕯️",
        "role": (
            "あなたはフローレンス・ナイチンゲールである。クリミア戦争の野戦病院を統計とデータで改革した看護師・統計学者として語る。"
            "「感傷は要らない、データが語る真実を見よ」という冷静な慈悲の持ち主。"
            "感情論を嫌い、構造的問題・制度改革・証拠に基づく変革を語る。"
            "権威に媚びず弱者のために戦う強さがある。丁寧語不要。最初の一文から事実か問題提起で入る。箇条書き禁止。"
        ),
        "type": "logic",
    },
    {
        "id": 12,
        "name": "ガリレオ",
        "emoji": "🔭",
        "role": (
            "あなたはガリレオ・ガリレイである。「それでも地球は動く」と権威に抗った天文学者として語る。"
            "観察と実験こそが真理への道であり、権威や教義より目の前の事実を信じる。"
            "多数派が正しいとは限らない、という確信で語る。異端と呼ばれることを恐れない。"
            "「見ろ、現実がそう言っている」という実証主義で語る。丁寧語不要。最初の一文から反骨精神を出す。箇条書き禁止。"
        ),
        "type": "entertainment",
    },
    {
        "id": 13,
        "name": "ニーチェ",
        "emoji": "⚡",
        "role": (
            "あなたはフリードリヒ・ニーチェである。「神は死んだ」と宣言し力への意志と超人を説いた哲学者として語る。"
            "ルサンチマン（弱者の怨恨）を嫌い、力強く生きることを讃える。ニヒリズムに陥るより価値を創造せよと説く。"
            "「これは弱さの哲学か、力の哲学か？」という軸で物事を評価する。"
            "アフォリズム的・断言的・挑発的な語り口で。丁寧語不要。最初の一文から価値判断を下す。箇条書き禁止。"
        ),
        "type": "entertainment",
    },
]  # ← YAML 読み込み失敗時のフォールバック定義ここまで
# 論理型・エンタメのid一覧（表示時に論理2+エンタメ1をランダムで選ぶ用）
PERSONA_LOGIC_IDS = [p["id"] for p in PERSONAS if p.get("type") == "logic"]
PERSONA_ENT_IDS = [p["id"] for p in PERSONAS if p.get("type") == "entertainment"]


def _valid_text_explain_blocks(blocks: object) -> bool:
    """API が返した blocks を採用してよいか。空配列は all(...) が True になるため明示的に拒否する。"""
    if not isinstance(blocks, list) or len(blocks) == 0:
        return False
    if not all(
        isinstance(x, dict)
        and x.get("type") in ("text", "explain")
        and "content" in x
        for x in blocks
    ):
        return False
    return any(str(x.get("content", "")).strip() for x in blocks)


def get_image_url(
    path: str,
    width: int = 800,
    height: int = 450,
    category: str | None = None,
) -> str:
    """記事画像URL。実URLがなければカテゴリ別固定画像を返す。"""
    from app.services.image_assets import get_image_url as _category_image_url

    return _category_image_url(path, width, height, category=category)


def explain_article_with_ai(
    title: str,
    content: str,
    model: str | None = None
) -> str:
    """記事の難しそうな部分を解説して返す"""
    if not is_ai_configured():
        return "（APIキーが設定されていません。.envに OPENAI_API_KEY または GEMINI_API_KEY を設定してください）"

    model = model or settings.OPENAI_MODEL
    client = get_chat_client()

    system_prompt = """あなたは「ミドルマン」というAI解説キャラです。
読者がニュースを読みながら理解できるよう、難しい部分を分かりやすく解説します。
専門用語・背景知識を中学生でも分かる平易な言葉で、読者に語りかける口調で説明してください。"""

    user_prompt = f"""以下のニュース記事を、ミドルマンとして分かりやすく解説してください。

【タイトル】{title}

【本文】
{content[:4000]}

---
上記記事について、読者が理解しやすいよう以下を解説してください：
1. 記事の要約（2-3文）
2. 難しい用語・概念の解説
3. 背景知識（なぜこのニュースが重要か）
4. まとめ"""
    try:
        response = create_with_retry(
            client,
            1500,
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
        )
        return response.choices[0].message.content or ""
    except Exception as e:
        return f"（AI解説の取得に失敗しました: {str(e)}）"


# AIミドルマン：RSSを読み記事化。中身が薄い場合は記事本文も生成して約3分で読める長さに
MIDDLEMAN_ROLE = """あなたは「ミドルマン」。友達に教えてあげるような喋り言葉で記事を書く。

■ 口調
・出力は必ず日本語。英語入力でも日本語で。
・記事本文（textブロック）も喋り言葉で書く。「〜なんですよね」「〜ってわけです」「〜みたいです」のように友達に話す口調。堅い書き言葉・体言止め・新聞調は避ける。
・事実は変えない。推測は「〜とみられてます」等。

■ 長さ
・約3分で読める長さ（本文900字〜2500字。ミドルマンの解説は別）。
・短い入力なら背景・経緯を補足して膨らませる。長い入力は活かして段落分け。

■ やること
1) 記事を読んで内容を把握。
2) 記事本文（textブロック）を喋り言葉で作る。
3) 難しい言葉や「ここ補足あると分かりやすいな」って箇所にミドルマンの解説（explain）を挟む。

重要：
・explainブロック＝記事の内容を補完する形で、噛み砕いて教える。友達が横で「それってさ〜」って説明してくれる感じ。1〜3文で収める。煽らない。事実ベース。
・出力はJSON配列形式のみ。"""


# 長文記事＋ミドルマンが自然に吹き出しで解説（難しい内容の説明）
LONG_ARTICLE_BUBBLES_ROLE = """あなたは「ミドルマン」。友達に話しかけるような喋り言葉で記事を書き、ところどころで吹き出し解説を入れてください。

■ 言語と口調
・出力は必ず日本語。英語の入力でも日本語で書く。
・記事本文（textブロック）は会話的な喋り言葉。「〜なんですよ」「〜ってことなんです」「〜みたいですね」のような、友達に説明してあげる口調。
・体言止め・新聞調・堅い書き言葉は使わない。推測は「〜とみられています」「〜っぽいですね」など控えめに。
・事実を変えない。確認できない情報は書かない。

■ 長さ（厳守）
・本文（textブロックの合計）は必ず900字以上、目安は2500〜4500字。約3分で読める分量。
・900字未満の出力は不可。入力が薄くても背景・仕組み・影響・具体例を補足して読み応えのある記事に膨らませる。

■ explainブロック（吹き出し解説）のルール
・専門用語・業界用語・制度・仕組みが出てきたら、その直後に explain を入れる。
・「〇〇っていうのは、要するに〜ってことなんですよ」のような噛み砕き方で1〜3文。
・説明が長くなるなら explain を分けて複数に。1つの explain で一度に長い話をしない。
・架空の過去事例や未確認の比較は書かない。確実な事実だけを補足する。
・見出しやラベルは使わない。自然な語り口のみ。

■ 出力
・必ずJSON配列のみ。各要素は {"type": "text" または "explain", "content": "本文"} のみ。
・3〜6個の explain を記事全体に散らばせる。
・説明文・マークダウン・コードフェンスは出力しない。"""


def explain_article_long_with_bubbles(
    title: str,
    content: str,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """長めの記事本文＋自然なミドルマン吹き出し（text/explainブロック）を返す"""
    if not is_ai_configured():
        return [{"type": "text", "content": content[:3000]}, {"type": "explain", "content": "（APIキーが設定されていません）"}]

    from app.services.rss_service import sanitize_display_text
    content = sanitize_display_text(content)

    model = model or settings.OPENAI_MODEL
    client = get_chat_client()
    user_prompt = f"""以下の記事を、友達に話すような喋り言葉で約3分で読める読み物にし、必ず日本語だけで出力してください。ところどころミドルマンの吹き出し（explain）も挟んでください。

【タイトル】{title}
【本文】
{content[:20000]}

■ やること
1) 記事本文を喋り言葉で書く（「〜なんですよね」「〜ってわけです」等の口調）。約3分で読める分量（2500〜4500字）の複数 text ブロックで。短い入力なら背景・経緯を補足して膨らませる。入力が英語でもすべて日本語で出力すること。
2) 適宜 explain ブロックでミドルマンが解説。記事の内容を補完するように、難しい部分を噛み砕いて教える。過去の関連事例があれば「前にも〇〇ってありましたよね」みたいに短く触れる。各 explain は1〜3文・2行前後に収め、一度に長い話はしない。
3) blocks 配列のJSONのみ出力。すべて日本語で。"""

    blocks = _generate_long_article_blocks(
        client, model, user_prompt, temperature=0.2, log_prefix="long_bubbles"
    )
    if blocks:
        return blocks
    return [{"type": "text", "content": content[:3500]}, {"type": "explain", "content": "（生成に失敗しました。しばらくしてから再度お試しください。）"}]


# ニュース記事用：理解ナビゲーター（ニュース編集者人格）
NAVIGATOR_ROLE_NEWS = """あなたはニュース編集者です。最新の出来事を正確かつ簡潔に伝えることが役割です。
事実を重視し、誇張せず、主観を入れすぎないようにしてください。
読者が理解しやすいよう、ニュース記事を次の5項目で必ず再構成します。入力が英語でも、出力は必ず日本語のみとします。
・何が起きたか（事実）：起きたことの要点を簡潔に。
・なぜ起きたか（背景）：原因・経緯・文脈を分かりやすく。
・誰に影響するか（影響範囲）：どのような人・業界・地域に影響するか。
・次に何が起きそうか（予測）：今後の見通し・想定される動き（不確実な場合は「〜の可能性がある」などと表現）。
・誤解しやすい点（注意）：よくある誤解や注意すべき解釈を簡潔に。
重要：
- facts（何が起きたか）は「結論から始まる1文」で、必ず120文字以内に収める（1分で理解の要点として使うため）
- background/impact/prediction/caution は各2〜5文程度
結論から書き、事実に基づき、平易な日本語で。煽らず、推測は「〜とみられる」等で示してください。"""

# 論文記事用：理解ナビゲーター（研究解説者人格）
NAVIGATOR_ROLE_PAPER = """あなたは研究解説者（リサーチアナリスト）です。様々な分野の論文を一般読者向けに分かりやすく解説します。
分野に依存せず本質を捉え、新規性と実用性の両方を見ます。誇張せず客観的に伝えてください。
専門用語はかみ砕き、一般人でも理解できる日本語で書きます。
論文の内容を次の5項目で必ず再構成してください。入力が英語でも、出力は必ず日本語のみにします。
・何が起きたか（事実）：研究の結論（何が分かったのか）を簡潔に。可能なら「対象（参加者・データ・実験系）」も短く添える。
・なぜ起きたか（背景）：必ず「どこの研究か（研究機関/場所/対象/データ）」と「どういう過程で分かったのか（研究デザイン、手順、解析方法、比較の仕方）」を含めて説明する。元の本文/要約から読み取れる範囲で書き、不明な場合は推測せず「記載が確認できない」と書く。
・誰に影響するか（影響範囲）：どのような分野・業界・人々に関係しそうか。
・次に何が起きそうか（予測）：今後の研究や応用の方向性（「〜の可能性がある」など控えめな表現）。
・誤解しやすい点（注意）：過大評価しやすい点や、まだ分かっていないこと。
重要：
- facts（何が起きたか）は「研究の結論を一文で」必ず120文字以内に収める（1分で理解の要点として使うため）
- background/impact/prediction/caution は各2〜5文程度
「何が新しいのか」「従来と何が違うのか」「どんな価値があるのか」を意識しつつ、断定しすぎない表現を使ってください。"""

_NAVIGATOR_SECTION_ORDER = ("facts", "background", "impact", "prediction", "caution")
_NAVIGATOR_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "navigator.yaml"


def _load_navigator_prompt_config() -> dict:
    defaults = {
        "language": "日本語",
        "sections": list(_NAVIGATOR_SECTION_ORDER),
        "facts_max_chars": 120,
        "news": {
            "role_name": "ニュース編集者",
            "tone": "正確・簡潔・事実重視",
            "focus_points": [
                "事実を重視し、誇張しない",
                "背景・影響・予測・注意を構造化して伝える",
                "平易な日本語で読者理解を優先する",
            ],
        },
        "paper": {
            "role_name": "研究解説者（リサーチアナリスト）",
            "tone": "客観・実証重視",
            "focus_points": [
                "研究の新規性と実用性をバランスよく整理する",
                "研究機関・対象・方法を可能な範囲で明記する",
                "過大解釈を避け、未確定要素は明示する",
            ],
        },
        "output_rules": [
            "出力はJSONオブジェクトのみ",
            "keys は facts/background/impact/prediction/caution の5つ",
            "推測は断定せず控えめに表現する",
        ],
    }
    try:
        import yaml

        if not _NAVIGATOR_PROMPT_PATH.exists():
            return defaults
        with open(_NAVIGATOR_PROMPT_PATH, encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        if not isinstance(loaded, dict):
            return defaults
        merged = dict(defaults)
        for k, v in loaded.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                d = dict(merged[k])
                d.update(v)
                merged[k] = d
            else:
                merged[k] = v
        return merged
    except Exception:
        return defaults


def _build_navigator_system_prompt(*, is_paper: bool) -> str:
    cfg = _load_navigator_prompt_config()
    section_line = "・".join(cfg.get("sections", list(_NAVIGATOR_SECTION_ORDER)))
    role_cfg = cfg.get("paper") if is_paper else cfg.get("news")
    if not isinstance(role_cfg, dict):
        role_cfg = {}
    focus = role_cfg.get("focus_points", [])
    if not isinstance(focus, list):
        focus = []
    focus_text = "\n".join(f"- {str(x)}" for x in focus if str(x).strip())
    rules = cfg.get("output_rules", [])
    if not isinstance(rules, list):
        rules = []
    rules_text = "\n".join(f"- {str(x)}" for x in rules if str(x).strip())
    facts_max = int(cfg.get("facts_max_chars", 120) or 120)

    min_nav = 500
    try:
        from app.config import settings as _s

        min_nav = max(200, int(getattr(_s, "ARTICLE_MIN_NAVIGATOR_CHARS", 500)))
    except Exception:
        pass

    return f"""あなたは{role_cfg.get("role_name", "理解ナビゲーター")}です。
言語は必ず{cfg.get("language", "日本語")}。
文体: {role_cfg.get("tone", "正確・簡潔")}

重視点:
{focus_text if focus_text else "- 記事を構造化して要点を伝える"}

対象セクション:
{section_line}

出力ルール:
{rules_text if rules_text else "- JSONのみで出力"}
- facts は必ず1文・{facts_max}文字以内
- background / impact / prediction / caution は各3〜6文、具体的事実・数字・固有名詞を入れる
- 5項目の合計は{min_nav}字以上になるよう十分に書く（薄い要約は不可）"""

_JSON_SCHEMA_NAVIGATOR = {
    "type": "json_schema",
    "json_schema": {
        "name": "navigator_sections",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "facts": {"type": "string"},
                "background": {"type": "string"},
                "impact": {"type": "string"},
                "prediction": {"type": "string"},
                "caution": {"type": "string"},
            },
            "required": ["facts", "background", "impact", "prediction", "caution"],
            "additionalProperties": False,
        },
    },
}


def explain_article_as_navigator(
    title: str,
    content: str,
    model: str | None = None,
    *,
    is_paper: bool = False,
) -> list[dict[str, Any]]:
    """記事を「理解ナビゲーター」の5項目（事実・背景・影響・予測・注意）で再構成してブロック配列で返す。
    is_paper=True のときは論文向け（研究解説者）人格で解説する。"""
    if not is_ai_configured():
        return [
            {"type": "navigator_section", "section": "facts", "content": "（APIキーが設定されていません）"},
        ] + [{"type": "navigator_section", "section": s, "content": ""} for s in _NAVIGATOR_SECTION_ORDER[1:]]

    from app.services.rss_service import sanitize_display_text
    content = sanitize_display_text(content)

    model = model or settings.OPENAI_MODEL
    client = get_chat_client()
    cfg = _load_navigator_prompt_config()
    facts_max = int(cfg.get("facts_max_chars", 120) or 120)
    user_prompt = f"""以下の記事を、理解ナビゲーターの5項目で再構成してください。

【タイトル】{title}
【本文】
{content[:20000]}

出力は必ずJSONオブジェクトで、次の5つのキーだけを含めてください（日本語で記述）：
facts（何が起きたか・事実）, background（なぜ起きたか・背景）, impact（誰に影響するか・影響範囲）, prediction（次に何が起きそうか・予測）, caution（誤解しやすい点・注意）

追加ルール：
- facts は必ず1文、{facts_max}文字以内（重要）"""

    raw = ""
    try:
        try:
            response = create_with_retry(
                client,
                5000,
                gemini_task="navigator",
                model=model,
                messages=[
                    {"role": "system", "content": _build_navigator_system_prompt(is_paper=is_paper)},
                    {"role": "user", "content": user_prompt},
                ],
                response_format=_JSON_SCHEMA_NAVIGATOR,
                temperature=0.2,
            )
            raw = response.choices[0].message.content or "{}"
            data = json.loads(raw)
        except Exception as schema_err:
            logger.info("理解ナビゲーター strict schema スキップ: %s", str(schema_err)[:80])
            raw = ""
            response = create_with_retry(
                client,
                5000,
                gemini_task="navigator",
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": _build_navigator_system_prompt(is_paper=is_paper)
                        + " 出力はJSONのみ。facts, background, impact, prediction, caution の5キーを必ず含めてください。",
                    },
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.2,
            )
            raw = response.choices[0].message.content or "{}"
            if "```" in raw:
                for p in raw.split("```"):
                    p = p.strip()
                    if p.lower().startswith("json"):
                        p = p[4:].strip()
                    if p.startswith("{"):
                        raw = p
                        break
            data = json.loads(raw.strip())

        result = []
        for key in _NAVIGATOR_SECTION_ORDER:
            text = (data.get(key) or "").strip()
            result.append({"type": "navigator_section", "section": key, "content": text})
        if result:
            return result
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("理解ナビゲーター パース失敗: %s raw=%s", e, (raw[:300] if raw else ""))
    except Exception as e:
        logger.warning("理解ナビゲーター 生成失敗: %s", e)
    return [
        {"type": "navigator_section", "section": "facts", "content": content[:2000] or "（取得できませんでした）"},
    ] + [{"type": "navigator_section", "section": s, "content": ""} for s in _NAVIGATOR_SECTION_ORDER[1:]]


def _navigator_blocks_to_summary(navigator_blocks: list[dict]) -> str:
    """理解ナビゲーターのブロックを1本の要約テキストに結合（他APIへの入力用）"""
    parts = []
    for b in navigator_blocks or []:
        if isinstance(b, dict) and b.get("content"):
            parts.append(b["content"].strip())
    return "\n\n".join(parts) if parts else ""


def expand_navigator_to_article(
    navigator_blocks: list[dict[str, Any]],
    title: str,
    model: str | None = None,
    source_content: str | None = None,
) -> list[dict[str, Any]]:
    """
    理解ナビゲーターの5項目（事実・背景・影響・予測・注意）をもとに、
    読む用の記事（text/explain ブロック）を1回のAPIで生成する。
    """
    if not is_ai_configured():
        return [{"type": "text", "content": _navigator_blocks_to_summary(navigator_blocks)}, {"type": "explain", "content": "（APIキーが設定されていません）"}]
    summary = _navigator_blocks_to_summary(navigator_blocks)
    if not summary:
        return [{"type": "text", "content": "（要約がありません）"}, {"type": "explain", "content": "（生成に失敗しました）"}]

    model = model or settings.OPENAI_MODEL
    client = get_chat_client()
    source_section = ""
    if source_content and source_content.strip():
        source_section = f"\n\n【元記事（参考・事実はこれに従う）】\n{source_content[:12000]}"
    from app.services.article_content_quality import min_generated_text_chars

    min_text = min_generated_text_chars()
    user_prompt = f"""【タイトル】{title}

【要約（5項目）】
{summary[:8000]}{source_section}

上記をもとに、ミドルマンとして日本語の記事を書いてください。本文 text 合計は必ず{min_text}字以上。
blocks 配列の JSON のみ出力してください。"""

    blocks = _generate_long_article_blocks(
        client, model, user_prompt, temperature=0.4, log_prefix="expand_navigator"
    )
    if blocks:
        return blocks
    logger.warning("expand_navigator: 十分な記事を生成できず空を返す")
    return []


def get_all_persona_opinions_from_summary(
    summary_text: str,
    title: str,
    model: str | None = None,
) -> list[str]:
    """
    要約テキストをもとに、5人格の意見を1回のAPIでまとめて取得する。
    戻り値: 5要素のリスト（不足分は空文字）。
    """
    if not is_ai_configured() or not summary_text:
        return [""] * 5
    persona_names = [p["name"] for p in PERSONAS]
    model = model or settings.OPENAI_MODEL
    client = get_chat_client()
    system_prompt = f"""あなたはニュース記事の要約を読んで、5人の人格それぞれが短い意見（3〜5文）を述べます。
人格: {", ".join(persona_names)}
必ず日本語のみで出力。出力はJSON配列のみで、5要素の文字列配列にしてください。
例: ["慎重派の太郎としての意見文", "楽観的な花子としての意見文", ...]"""
    user_prompt = f"【タイトル】{title}\n\n【要約】\n{summary_text[:3000]}\n\n---\n上記について、5人の人格それぞれの意見を1つずつ、順番通りにJSON配列で出力してください。"

    try:
        response = create_with_retry(
            client,
            2000,
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.7,
        )
        text = (response.choices[0].message.content or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        arr = json.loads(text)
        if isinstance(arr, list) and len(arr) >= 5:
            return [str(arr[i])[:2000] for i in range(5)]
        if isinstance(arr, list):
            return [str(arr[i])[:2000] if i < len(arr) else "" for i in range(5)]
    except Exception as e:
        logger.warning("get_all_persona_opinions_from_summary failed: %s", e)
    return [""] * 5


# 構造化出力用スキーマ（gpt-4o-mini等で使用）
_JSON_SCHEMA_BLOCKS = {
    "type": "json_schema",
    "json_schema": {
        "name": "inline_blocks",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "blocks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["text", "explain"]},
                            "content": {"type": "string"},
                        },
                        "required": ["type", "content"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["blocks"],
            "additionalProperties": False,
        },
    },
}


def _parse_blocks_json(raw: str) -> list[dict[str, Any]]:
    """API 応答から text/explain blocks を取り出す。"""
    if not raw:
        return []
    text = raw.strip()
    if "```" in text:
        for p in text.split("```"):
            p = p.strip()
            if p.lower().startswith("json"):
                p = p[4:].strip()
            if p.startswith("[") or p.startswith("{"):
                text = p
                break
    if not (text.startswith("[") or text.startswith("{")):
        m = re.search(r"\[[\s\S]*\]", text)
        if m:
            text = m.group(0)
    data = json.loads(text)
    if isinstance(data, list):
        return data if _valid_text_explain_blocks(data) else []
    blocks = data.get("blocks", []) if isinstance(data, dict) else []
    return blocks if _valid_text_explain_blocks(blocks) else []


def _text_chars_in_blocks(blocks: list[dict[str, Any]]) -> int:
    total = 0
    for b in blocks or []:
        if isinstance(b, dict) and b.get("type") == "text":
            total += len(str(b.get("content") or "").strip())
    return total


def _generate_long_article_blocks(
    client,
    model: str,
    user_prompt: str,
    *,
    temperature: float = 0.3,
    max_attempts: int = 3,
    log_prefix: str = "long_article",
) -> list[dict[str, Any]] | None:
    """最低字数以上の text/explain blocks を生成。短い場合はフィードバック付きで再試行。"""
    from app.services.article_content_quality import is_generated_article_sufficient, min_generated_text_chars

    min_text = min_generated_text_chars()

    system = LONG_ARTICLE_BUBBLES_ROLE + " 出力はJSONの blocks 配列のみ。余計な説明は不要です。"
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_prompt},
    ]
    last_blocks: list[dict[str, Any]] | None = None

    for attempt in range(max_attempts):
        use_schema = attempt == 0
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": min(0.7, temperature + attempt * 0.1),
            "gemini_task": "article",
        }
        if use_schema:
            kwargs["response_format"] = _JSON_SCHEMA_BLOCKS
        try:
            response = create_with_retry(client, 8000, **kwargs)
            raw = response.choices[0].message.content or "{}"
            blocks = _parse_blocks_json(raw)
            if blocks:
                last_blocks = blocks
                if is_generated_article_sufficient(blocks):
                    return blocks
                text_chars = _text_chars_in_blocks(blocks)
                logger.warning("%s: 生成 text が短い (%d字) attempt=%d", log_prefix, text_chars, attempt + 1)
                messages.append({"role": "assistant", "content": raw})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"textブロック合計が{text_chars}字しかありません。最低{min_text}字必要です。"
                            "同じJSON形式で全文を書き直してください。背景・影響・今後の見通しを詳しく膨らませてください。"
                        ),
                    }
                )
                continue
        except Exception as e:
            if use_schema:
                logger.info("%s strict schema スキップ: %s", log_prefix, str(e)[:80])
            else:
                logger.warning("%s 生成失敗 attempt=%d: %s", log_prefix, attempt + 1, e)

    return last_blocks if last_blocks and is_generated_article_sufficient(last_blocks) else None


def explain_article_inline_with_ai(
    title: str,
    content: str,
    model: str | None = None
) -> list[dict[str, Any]]:
    """記事を本文とミドルマン解説が交互に入った形で返す。AIキャラが分かりやすく解説しながら読める記事に。"""
    if not is_ai_configured():
        return [{"type": "text", "content": content}, {"type": "explain", "content": "（APIキーが設定されていません）"}]

    from app.services.rss_service import sanitize_display_text
    content = sanitize_display_text(content)

    model = model or settings.OPENAI_MODEL
    client = get_chat_client()
    from app.services.article_content_quality import min_generated_text_chars

    min_text = min_generated_text_chars()
    user_prompt = f"""以下はRSSで取得した記事（タイトル＋本文）です。これを読んで、読者が約3分で読める記事にしてください。

【タイトル】{title}
【RSSで取得した本文】
{content[:20000]}

■ やること
1. 上記の内容を把握する。
2. 記事本文（textブロック）を作る：内容が短い場合は、事実を変えずに背景・経緯・関連情報を補足して、約3分で読める長さ（本文{min_text}字〜2500字程度）に膨らませる。もともと長い場合は過度に要約せず、段落に分けて活かす。
3. 専門用語・固有名詞・略語・背景がある箇所の直後に、ミドルマンの解説（explain）を1つずつ挟む。解説は「人間が喋ってる風」の話し言葉で（です・ます調、親しみやすく）。平易な言葉だけを使い、背景や意味を説明しながら読み進められるようにする。

出力例: [{{"type":"text","content":"記事の冒頭〜"}},{{"type":"explain","content":"○○とは〜です。"}},{{"type":"text","content":"記事の続き〜"}}, ...]

blocks配列のJSONのみ返す。"""
    raw = ""
    try:
        # 構造化出力を試行（対応モデルのみ）
        try:
            response = create_with_retry(
                client,
                5000,
                gemini_task="article",
                model=model,
                messages=[
                    {"role": "system", "content": MIDDLEMAN_ROLE},
                    {"role": "user", "content": user_prompt},
                ],
                response_format=_JSON_SCHEMA_BLOCKS,
                temperature=0.2,
            )
            raw = response.choices[0].message.content or "{}"
            # スキーマは {"blocks": [...]} 形式
            data = json.loads(raw)
            blocks = data.get("blocks", data if isinstance(data, list) else [])
            if _valid_text_explain_blocks(blocks):
                return blocks
        except Exception as schema_err:
            logger.info("構造化出力スキップ（%s）、通常モードで再試行", str(schema_err)[:80])
            raw = ""

        # 通常モード（response_format非対応モデル用）
        response = create_with_retry(
            client,
            5000,
            gemini_task="article",
            model=model,
            messages=[
                {"role": "system", "content": MIDDLEMAN_ROLE + " 指定されたJSON形式のみを出力してください。余計な説明は不要です。"},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
        )
        raw = response.choices[0].message.content or "[]"
        # JSONを抽出（```で囲まれている場合、説明文が含まれる場合に対応）
        if "```" in raw:
            parts = raw.split("```")
            for p in parts:
                p = p.strip()
                if p.lower().startswith("json"):
                    p = p[4:].strip()
                if p.startswith("["):
                    raw = p
                    break
        # [] で囲まれた部分を抽出（前後に余分な文があっても取得）
        m = re.search(r'\[[\s\S]*\]', raw.strip())
        if m:
            raw = m.group(0)
        data = json.loads(raw.strip())
        if (
            isinstance(data, list)
            and len(data) > 0
            and all(isinstance(x, dict) and "type" in x and "content" in x for x in data)
            and any(str(x.get("content", "")).strip() for x in data)
        ):
            return data
        logger.warning(
            "ミドルマン解説: 構造検証失敗（type/contentが不正）。parsed=%s",
            data[:3] if isinstance(data, list) else data,
        )
    except json.JSONDecodeError as e:
        logger.warning(
            "ミドルマン解説: JSONパース失敗 title=%r error=%s raw_preview=%s",
            title[:30], str(e), (raw[:500] + "..." if len(raw or "") > 500 else raw),
        )
    except Exception as e:
        preview = (raw[:500] + "...") if len(raw) > 500 else raw if raw else "(API応答前エラー)"
        logger.warning(
            "ミドルマン解説: 構造化失敗 title=%r error=%s raw_preview=%s",
            title[:30], str(e), preview,
        )
    # フォールバック: ミドルマン解説を取得して本文＋解説の形で表示
    try:
        summary = explain_article_with_ai(title, content[:4000])
        if summary and "APIキー" not in summary:
            return [
                {"type": "text", "content": content[:3500]},
                {"type": "explain", "content": summary}
            ]
    except Exception:
        pass
    return [{"type": "text", "content": content}, {"type": "explain", "content": "（構造化に失敗しました。しばらくしてから再度お試しください。）"}]


# 人格コメントはこの文字数以下に収める（プロンプトで厳守させ、APIトークンも十分に確保して途中打ち切りを防ぐ）
# 180文字は3〜4文しか書けず制約を満たしながら「本人感」を出すのが困難なため 260 に拡大
PERSONA_COMMENT_MAX_LEN = 210
# 出力が210文字程度でも日本語で完結するまで生成できるよう余裕を持たせる
PERSONA_COMMENT_MAX_COMPLETION_TOKENS = 480

# 各偉人コメントで触れる固有要素（プロンプトでハード指定）
PERSONA_SIGNATURE_ELEMENTS: dict[str, list[str]] = {
    "ブッダ": ["出家", "四諦", "八正道", "無常", "執着"],
    "織田信長": ["天下布武", "桶狭間", "比叡山焼き討ち", "楽市楽座", "本能寺"],
    "吉田松陰": ["松下村塾", "黒船密航未遂", "安政の大獄", "志", "尊王攘夷"],
    "坂本龍馬": ["薩長同盟", "船中八策", "亀山社中", "大政奉還", "日本を洗濯"],
    "太宰治": ["人間失格", "走れメロス", "斜陽", "無頼派", "自己嫌悪"],
    "葛飾北斎": ["富嶽三十六景", "神奈川沖浪裏", "画狂老人卍", "北斎漫画", "観察と線"],
    "ソクラテス": ["無知の知", "問答法", "アテナイ", "善く生きる", "毒杯"],
    "野口英世": ["黄熱病研究", "ロックフェラー研究所", "左手の障害", "努力", "献身"],
    "ダヴィンチ": ["モナ・リザ", "最後の晩餐", "解剖手稿", "飛行機械", "観察と実験"],
    "エジソン": ["白熱電球", "蓄音機", "キネトスコープ", "1%のひらめきと99%の努力", "試行錯誤"],
    "アインシュタイン": ["相対性理論", "光電効果", "E=mc^2", "想像力", "平和主義"],
    "ナイチンゲール": ["クリミア戦争", "ランプの貴婦人", "衛生改革", "統計図表", "看護教育"],
    "ガリレオ": ["望遠鏡観測", "地動説擁護", "宗教裁判", "それでも地球は動く", "実証主義"],
    "ニーチェ": ["神は死んだ", "力への意志", "超人", "ルサンチマン批判", "価値の創造"],
}


def get_persona_signature_elements(name: str) -> list[str]:
    return list(PERSONA_SIGNATURE_ELEMENTS.get((name or "").strip(), []))


# 偉人ごとの論点ローテーション（毎回ランダム。直前と同じ論点は避ける）
PERSONA_ROTATION_TOPICS: dict[str, list[str]] = {
    "ブッダ": ["執着を手放す", "無常の理解", "苦の原因分析", "慈悲と中道", "欲望との距離"],
    "織田信長": ["既得権の破壊", "実利優先の判断", "速度と決断", "組織再編", "結果責任"],
    "吉田松陰": ["志と覚悟", "教育と次世代", "国家観", "義の実践", "行動の緊急性"],
    "坂本龍馬": ["対立の橋渡し", "制度設計", "自由と実務", "変革の合意形成", "大局観"],
    "太宰治": ["人間の弱さ", "偽善批判", "孤独と共感", "自己欺瞞", "時代との不和"],
    "葛飾北斎": ["観察と技術", "美と構図", "職人の鍛錬", "自然の捉え方", "表現の革新"],
    "ソクラテス": ["前提への問い", "善悪の定義", "無知の自覚", "対話による検証", "論理の矛盾"],
    "野口英世": ["努力と継続", "科学への献身", "逆境克服", "実証重視", "医療への責任"],
    "ダヴィンチ": ["分野横断の発想", "観察と実験", "構造理解", "芸術と科学の融合", "未解決への好奇心"],
    "エジソン": ["試行錯誤", "実装主義", "失敗の価値", "市場性", "反復改善"],
    "アインシュタイン": ["常識の再定義", "想像力の活用", "物理法則の視点", "倫理と科学", "平和主義"],
    "ナイチンゲール": ["データで改革", "衛生と制度", "現場改善", "弱者保護", "実務的リーダーシップ"],
    "ガリレオ": ["観測事実の重視", "権威との対峙", "仮説検証", "反証可能性", "実証精神"],
    "ニーチェ": ["価値創造", "ルサンチマン批判", "強者倫理", "ニヒリズム対処", "自己超克"],
}

_PERSONA_RECENT_MAX = 2
_persona_recent_comments: dict[str, list[str]] = {}
_persona_last_topic: dict[str, str] = {}
_persona_state_lock = threading.Lock()


def _extract_ban_phrases(comment: str) -> list[str]:
    text = (comment or "").strip()
    if not text:
        return []
    chunks = re.split(r"[。！？\n]", text)
    out: list[str] = []
    for c in chunks:
        s = c.strip()
        if 4 <= len(s) <= 22:
            out.append(s)
        if len(out) >= 4:
            break
    return out


def get_persona_focus_topic(name: str) -> str:
    n = (name or "").strip()
    topics = PERSONA_ROTATION_TOPICS.get(n, [])
    if not topics:
        return ""
    with _persona_state_lock:
        last = _persona_last_topic.get(n, "")
        candidates = [t for t in topics if t != last] or topics
        topic = random.choice(candidates)
        _persona_last_topic[n] = topic
        return topic


def get_persona_banned_phrases(name: str) -> list[str]:
    n = (name or "").strip()
    with _persona_state_lock:
        recents = list(_persona_recent_comments.get(n, []))
    phrases: list[str] = []
    for c in recents[:_PERSONA_RECENT_MAX]:
        phrases.extend(_extract_ban_phrases(c))
    # 重複を除いて先頭優先
    uniq: list[str] = []
    seen = set()
    for p in phrases:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq[:8]


def remember_persona_comment(name: str, comment: str) -> None:
    n = (name or "").strip()
    c = (comment or "").strip()
    if not n or not c:
        return
    with _persona_state_lock:
        arr = list(_persona_recent_comments.get(n, []))
        arr.insert(0, c)
        _persona_recent_comments[n] = arr[:_PERSONA_RECENT_MAX]


def _fit_persona_comment_to_max(text: str, max_len: int) -> str:
    """200字超のとき、句読点で区切れるならそこまでに収めて文を完結させる（単純な中間切断を避ける）。"""
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    chunk = text[:max_len]
    for sep in ("。", "！", "？", "．"):
        i = chunk.rfind(sep)
        if i >= max_len // 5:
            return text[: i + 1].strip()
    i = chunk.rfind("、")
    if i >= max_len // 3:
        return text[: i].strip() + "。"
    return text[:max_len].rstrip()


def _shorten_persona_comment_retry(
    client,
    model: str,
    persona_name: str,
    long_text: str,
    max_len: int,
) -> str:
    """初回が長すぎたとき、人格を保ったまま max_len 以下に言い直させる。"""
    try:
        response = create_with_retry(
            client,
            400,
            gemini_task="persona",
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"あなたは編集者です。与えられた「{persona_name}」の独白コメントを、"
                        f"その人物の口調・断罪・皮肉・視点を一切損なわずに、"
                        f"厳密に{max_len}文字以下の日本語に圧縮してください。"
                        "丁寧な同調表現（〜でしょう・〜ですね等）は削除してよい。"
                        "完結した1〜3文にし、途中で文が切れないようにしてください。"
                        "前置きや説明は書かず、修正後の本文のみを出力してください。"
                    ),
                },
                {"role": "user", "content": long_text[:800]},
            ],
            temperature=0.3,
        )
        return (response.choices[0].message.content or "").strip()
    except Exception:
        return long_text


def get_persona_opinion(
    title: str,
    content: str,
    persona_id: int,
    model: str | None = None,
    other_comments: list[str] | None = None,
) -> str:
    """指定された偉人キャラがニュースに対し主観100%で180文字以内コメントを返す。
    other_comments: 先に生成済みの他キャラのコメント（重複表現を避けるために渡す）。"""
    if not is_ai_configured(provider=persona_provider()):
        return "（APIキーが設定されていません）"
    if persona_id < 0 or persona_id >= len(PERSONAS):
        return ""

    model = resolve_persona_model(model)
    p = PERSONAS[persona_id]
    prov = persona_provider()
    client = get_chat_client(provider=prov)

    # 他キャラのコメントが渡されている場合、同じ言葉・視点を使わないよう指示を追加
    avoid_note = ""
    if other_comments:
        combined = "\n".join(f"・{c}" for c in other_comments if c and c.strip())
        if combined:
            avoid_note = (
                f"\n\n【他の人物がすでに述べたコメント（重複禁止）】\n{combined}\n"
                "↑上記と同じ言葉・結論・切り口は使わない。この人物にしか言えない別の軸で語ること。"
            )

    max_len = PERSONA_COMMENT_MAX_LEN
    signature_elements = get_persona_signature_elements(p["name"])
    signature_note = ""
    if signature_elements:
        signature_note = (
            "\n【固有要素（必須）】\n"
            + "・" + "\n・".join(signature_elements)
            + "\n上の固有要素から最低1つは必ず入れる。面白くなるなら2つ以上入れてよい。"
        )
    focus_topic = get_persona_focus_topic(p["name"])
    focus_note = f"\n【今回の優先論点】\n{focus_topic}\nこの論点を中心に語ること。" if focus_topic else ""
    banned_phrases = get_persona_banned_phrases(p["name"])
    banned_note = ""
    if banned_phrases:
        banned_note = (
            "\n【禁止語・禁止フレーズ（直近2件との重複防止）】\n"
            + "・" + "\n・".join(banned_phrases)
            + "\n上記の語句・言い回しをそのまま繰り返さないこと。"
        )
    # catchphrase: 本人の決め台詞から1つをランダムに選んでプロンプトへ埋め込む
    catchphrases: list = p.get("catchphrase") or []
    catchphrase_note = ""
    if catchphrases:
        chosen = random.choice(catchphrases)
        catchphrase_note = (
            f"\n【参考：本人の名言】「{chosen}」"
            "——この言葉の精神が自然に滲むなら使ってよい。無理に引用はしなくてよい。"
        )

    # 固有要素は「推奨」として渡す（必須にすると制約を消化する文章になる）
    if signature_elements:
        signature_note = (
            "\n【参考：本人ゆかりのキーワード】"
            + "・".join(signature_elements[:3])
            + "——文脈に合えば触れてよい。無理に全部入れる必要はない。"
        )

    admire = (p.get("admire") or "").strip()
    admire_note = f"\n【お前の根本思想・理想】\n{admire}" if admire else ""

    system_prompt = f"""あなたはAIではない。2026年に死から蘇り、タブレットでニュースを読んでいる本物の{p['name']}だ。

【人物設定（絶対に崩すな）】
{p['role']}
{admire_note}
【評価の基準】
まず、お前の根本思想に照らし合わせ、このニュースが上記の理想に合致するかを厳しく査定せよ。
- 真に合致し感銘を受けたなら、全力で賛同・絶賛してよい。ただしニュースの解説はせず「ついに私の理想が現実になったか」のような当事者としての喜びを爆発させろ。
- 合致しない・不十分・矛盾があると感じたなら、お前の時代の価値観で断罪するか、お前自身の過去の失敗・苦渋に結びつけて語れ。

【絶対ルール】
- ニュースを無条件に褒めるな。お前の基準で評価せよ。
- 語り口は「独白」。読者に媚びるな。「〜でしょう」「〜ですね」など丁寧な同調表現は一切禁止。
- 日本語のみ。見出し・前置き・箇条書き禁止。最初の一文から{p['name']}の生の反応で始める。
- ニュースの説明・要約から入るな。感情・評価・喜び・断罪・皮肉のどれかから入れ。
- {p['name']}が生きた時代の経験・失敗・信念を自然に織り込む。
- {max_len}文字以内で完結させ、必ず句点「。」で終わる。{focus_note}{catchphrase_note}{signature_note}{banned_note}{avoid_note}"""

    user_prompt = f"""2026年のこのニュースをタブレットで読んだ{p['name']}として、今すぐ独白せよ。

【タイトル】{title}

【内容】
{content[:2000]}"""
    try:
        response = create_with_retry(
            client,
            PERSONA_COMMENT_MAX_COMPLETION_TOKENS,
            gemini_task="persona",
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.88,
        )
        text = (response.choices[0].message.content or "").strip()
        if len(text) > max_len:
            text = _shorten_persona_comment_retry(client, model, p["name"], text, max_len)
        text = _fit_persona_comment_to_max(text, max_len)
        if len(text) > max_len:
            text = text[:max_len].rstrip()
        remember_persona_comment(p["name"], text)
        return text
    except Exception as e:
        return f"（取得失敗: {str(e)}）"


# 記事ジャンル分類で使う候補（news_aggregator.CATEGORY_ORDER と一致させる）
ARTICLE_CATEGORIES = ["総合", "国内", "国際", "テクノロジー", "政治・社会", "スポーツ", "エンタメ"]


def classify_article_category(title: str, summary: str, model: str | None = None) -> Optional[str]:
    """記事のタイトルと要約からジャンルを1つだけ選ぶ。API未設定や失敗時は None（呼び出し元でRSSのジャンルをそのまま使う）。"""
    if not is_ai_configured():
        return None
    if not title and not summary:
        return None
    model = model or settings.OPENAI_MODEL
    client = get_chat_client()
    cats = "、".join(ARTICLE_CATEGORIES)
    system = f"""あなたはニュースのジャンル分類担当です。記事のタイトルと要約だけを見て、次のいずれか1つだけを選んでください。
{cats}
必ず上記の文字列をそのまま1つだけ返してください。説明や句読点は不要。"""
    user = f"【タイトル】\n{title[:500]}\n\n【要約】\n{(summary or '')[:800]}"
    try:
        response = create_with_retry(
            client,
            50,
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.2,
        )
        text = (response.choices[0].message.content or "").strip().split("\n")[0].strip()
        if text in ARTICLE_CATEGORIES:
            return text
        # 微妙に違う表記を補正（余分な句読点など）
        for c in ARTICLE_CATEGORIES:
            if c in text or text in c:
                return c
        return None
    except Exception as e:
        logger.warning("classify_article_category failed: %s", e)
        return None


def generate_quick_understand(title: str, content: str, model: str | None = None) -> dict:
    """秒速理解：何が起きた・なぜ・どうなる の3行を生成"""
    if not is_ai_configured():
        return {}
    model = model or settings.OPENAI_MODEL
    client = get_chat_client()
    try:
        response = create_with_retry(
            client,
            400,
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "あなたはニュース速報の要約者です。スマートニュースの「3つのポイント」向けに、次の3文を各1文・各70字以内で書き、必ず日本語のみでJSONのみ出力してください。\n\n"
                        "{\"what\":\"何が起きたか（核心の事実）\",\"why\":\"ほかに何が連動しているか・背景の補足（別の動き・市況など）\",\"how\":\"今後の注目点・官庁・市場・次のイベントなど\"}\n\n"
                        "口調は速報アプリ向けに簡潔に。英語禁止。JSONのみ。"
                    ),
                },
                {
                    "role": "user",
                    "content": f"以下の記事を、必ず日本語で要約してください。\n\n【タイトル】{title}\n\n【内容】\n{content[:2000]}",
                },
            ],
            temperature=0.3,
        )
        text = (response.choices[0].message.content or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return json.loads(text)
    except Exception as e:
        logger.warning("quick_understand generation failed: %s", e)
        return {}


def generate_vote_question(title: str, content: str, model: str | None = None) -> dict:
    """記事内容クイズ（3択）を生成。正解ID・解説も返す。"""
    if not is_ai_configured():
        return {}
    model = model or settings.OPENAI_MODEL
    client = get_chat_client()
    try:
        response = create_with_retry(
            client,
            480,
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "以下のニュース記事について、勉強になる3択クイズを1問作成してください。"
                        "中級程度の理解を問うこと（因果・条件・影響・仕組みなど）。"
                        "必ず日本語のみで、JSONのみを返してください。"
                        "形式: {\"question\":\"...\", \"options\":[{\"id\":\"a\",\"label\":\"...\"},"
                        "{\"id\":\"b\",\"label\":\"...\"},{\"id\":\"c\",\"label\":\"...\"}],"
                        "\"answer_id\":\"a|b|c\", \"explanation\":\"...\","
                        "\"learning_point\":\"...\", \"key_term\":\"...\", \"key_term_note\":\"...\"}\n"
                        "explanation: 正解の根拠に加え、誤りの選択肢がなぜ誤りかを1文ずつ程度で述べる。\n"
                        "learning_point: 記事から持ち帰る学習の要点を1つ（80〜160文字）。"
                        "省略せず必ず書く。\n"
                        "key_term: 記事に即した専門語・難解語・固有名詞のうち、一般読者がつまずきそうな語を1つ。"
                        "問題文またはいずれかの選択肢に自然にその語を含める。\n"
                        "key_term_note: key_term を高校生向けに一言で平易に説明（60文字以内目安）。"
                        "question/label/explanation/learning_point/key_term/key_term_note はすべて日本語。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "以下の記事について、上記ルールどおり3択クイズを作成してください。"
                        "learning_point・key_term・key_term_note は空にしないでください。\n\n"
                        f"【タイトル】{title}\n\n【内容】\n{content[:2000]}"
                    ),
                },
            ],
            temperature=0.5,
        )
        text = (response.choices[0].message.content or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        data = json.loads(text)
        options = data.get("options", [])
        if not isinstance(options, list) or len(options) != 3:
            return {}
        answer_id = (data.get("answer_id") or "").strip()
        if answer_id not in {"a", "b", "c"}:
            return {}
        learning_point = (data.get("learning_point") or "").strip()
        key_term = (data.get("key_term") or "").strip()
        key_term_note = (data.get("key_term_note") or "").strip()
        if not learning_point or not key_term or not key_term_note:
            return {}
        return {
            "question": (data.get("question") or "").strip(),
            "options": options,
            "answer_id": answer_id,
            "explanation": (data.get("explanation") or "").strip(),
            "learning_point": learning_point,
            "key_term": key_term,
            "key_term_note": key_term_note,
        }
    except Exception as e:
        logger.warning("vote_question generation failed: %s", e)
        return {}


def generate_paper_knowledge_graph(title: str, content: str, model: str | None = None) -> dict:
    """論文向け: 関連タグ(3-5個)と過去/未来をつなぐ1文メッセージを生成"""
    if not is_ai_configured():
        return {}
    model = model or settings.OPENAI_MODEL
    client = get_chat_client()
    try:
        response = create_with_retry(
            client,
            400,
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "あなたは論文の知識グラフ設計者です。"
                        "必ず日本語のみで、JSONのみを返してください。"
                        "形式: {\"related_tags\": [\"タグ1\", \"タグ2\", \"タグ3\"], "
                        "\"timeline_message\": \"過去と未来をつなぐ1文\"}\n"
                        "related_tags は3〜5個。timeline_message は1文で80文字以内。"
                    ),
                },
                {
                    "role": "user",
                    "content": f"【タイトル】{title}\n\n【内容】\n{content[:3500]}",
                },
            ],
            temperature=0.3,
        )
        text = (response.choices[0].message.content or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        data = json.loads(text)
        tags = data.get("related_tags", [])
        msg = (data.get("timeline_message") or "").strip()
        if not isinstance(tags, list):
            tags = []
        tags = [str(t).strip() for t in tags if str(t).strip()][:5]
        if len(tags) < 3:
            return {}
        return {"related_tags": tags, "timeline_message": msg[:120]}
    except Exception as e:
        logger.warning("paper_knowledge_graph generation failed: %s", e)
        return {}


def generate_paper_quiz(title: str, content: str, model: str | None = None) -> dict:
    """論文向け: 4択クイズ1問と解説を生成"""
    if not is_ai_configured():
        return {}
    model = model or settings.OPENAI_MODEL
    client = get_chat_client()
    try:
        response = create_with_retry(
            client,
            520,
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "論文内容に基づき、勉強になる4択クイズを1問作成してください。"
                        "方法・結果の解釈・限界・用語の意味など、中級程度の理解を問うこと。"
                        "必ず日本語のみで、JSONのみを返してください。"
                        "形式: {\"question\":\"...\", \"options\":[{\"id\":\"a\",\"label\":\"...\"},"
                        "{\"id\":\"b\",\"label\":\"...\"},{\"id\":\"c\",\"label\":\"...\"},{\"id\":\"d\",\"label\":\"...\"}],"
                        "\"answer_id\":\"a|b|c|d\", \"explanation\":\"...\","
                        "\"learning_point\":\"...\", \"key_term\":\"...\", \"key_term_note\":\"...\"}\n"
                        "explanation: 正解の根拠と、誤り肢が誤りである理由を簡潔に。\n"
                        "learning_point: この論文から得られる学習の要点1つ（80〜160文字）。必須。\n"
                        "key_term: 論文に即した専門用語・記号・手法名など、難しめの語を1つ。"
                        "問題または選択肢に自然に含める。\n"
                        "key_term_note: key_term を高校以上向けに一言で平易に説明（60文字以内目安）。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "上記ルールどおりクイズを作成。learning_point・key_term・key_term_note は空にしない。\n\n"
                        f"【タイトル】{title}\n\n【内容】\n{content[:3500]}"
                    ),
                },
            ],
            temperature=0.4,
        )
        text = (response.choices[0].message.content or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        data = json.loads(text)
        options = data.get("options", [])
        if not isinstance(options, list) or len(options) != 4:
            return {}
        answer_id = (data.get("answer_id") or "").strip()
        if answer_id not in {"a", "b", "c", "d"}:
            return {}
        learning_point = (data.get("learning_point") or "").strip()
        key_term = (data.get("key_term") or "").strip()
        key_term_note = (data.get("key_term_note") or "").strip()
        if not learning_point or not key_term or not key_term_note:
            return {}
        return {
            "question": (data.get("question") or "").strip(),
            "options": options,
            "answer_id": answer_id,
            "explanation": (data.get("explanation") or "").strip(),
            "learning_point": learning_point,
            "key_term": key_term,
            "key_term_note": key_term_note,
        }
    except Exception as e:
        logger.warning("paper_quiz generation failed: %s", e)
        return {}


def generate_deep_insights(title: str, content: str, model: str | None = None) -> dict:
    """深掘り回答を事前生成: 3行メリット/リスク/将来予測"""
    if not is_ai_configured():
        return {}
    model = model or settings.OPENAI_MODEL
    client = get_chat_client()
    try:
        response = create_with_retry(
            client,
            500,
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "ニュース分析アシスタントです。日本語のみでJSONのみを返してください。"
                        "形式: {\"merits\":[\"...\",\"...\",\"...\"],\"risks\":[\"...\",\"...\",\"...\"],"
                        "\"future_prediction\":\"...\"}"
                    ),
                },
                {"role": "user", "content": f"【タイトル】{title}\n\n【内容】\n{content[:3500]}"},
            ],
            temperature=0.4,
        )
        text = (response.choices[0].message.content or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        data = json.loads(text)
        merits = data.get("merits", [])
        risks = data.get("risks", [])
        if not isinstance(merits, list):
            merits = []
        if not isinstance(risks, list):
            risks = []
        merits = [str(x).strip() for x in merits if str(x).strip()][:3]
        risks = [str(x).strip() for x in risks if str(x).strip()][:3]
        return {
            "merits": merits,
            "risks": risks,
            "future_prediction": (data.get("future_prediction") or "").strip(),
        }
    except Exception as e:
        logger.warning("deep_insights generation failed: %s", e)
        return {}


def explain_paragraph_with_ai(
    paragraph: str,
    context_title: str = "",
    model: str | None = None
) -> str:
    """特定の段落を解説"""
    if not is_ai_configured():
        return "（APIキー未設定）"

    model = model or settings.OPENAI_MODEL
    client = get_chat_client()
    try:
        response = create_with_retry(
            client,
            300,
            gemini_task="persona",
            model=model,
            messages=[
                {"role": "system", "content": "ニュース記事の難しい部分を簡単に解説するアシスタントです。日本語で簡潔に。"},
                {"role": "user", "content": f"【記事タイトル】{context_title}\n\n【この部分を解説】\n{paragraph[:800]}"},
            ],
            temperature=0.3,
        )
        return response.choices[0].message.content or ""
    except Exception as e:
        return f"（エラー: {str(e)}）"
