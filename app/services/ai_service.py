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
            # 新フィールド正規化（存在しない場合のデフォルト設定）
            for _lf in ("lens", "humor_style", "favorite_moves", "avoid"):
                v = data.get(_lf)
                if v is None:
                    data[_lf] = []
                elif not isinstance(v, list):
                    data[_lf] = [str(v).strip()]
            if not isinstance(data.get("comment_angles"), list):
                data["comment_angles"] = []
            if not isinstance(data.get("comment_style"), dict):
                data["comment_style"] = {}
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


def get_image_url(path: str, width: int = 800, height: int = 450) -> str:
    """CDN経由で画像URLを生成（プレースホルダー用）"""
    if path and path.startswith("http"):
        return path
    seed = abs(hash(path or "")) % 10000 if path else 0
    return f"{settings.CDN_BASE_URL}/seed/{seed}/{width}/{height}"


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
・「〇〇っていうのは、要するに〜ってことなんですよ」のような噛み砕き方で1〜2文・80字以内（厳守）。
・1つの explain で長い話をしない。収まらないなら explain を分けて複数に。
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
2) 適宜 explain ブロックでミドルマンが解説。記事の内容を補完するように、難しい部分を噛み砕いて教える。各 explain は1〜2文・80字以内（厳守）。長くなるなら explain を分けること。
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
    """最低字数・日本語比率以上の text/explain blocks を生成。不足時はフィードバック付きで再試行。"""
    from app.services.article_content_quality import (
        blocks_mainly_japanese,
        is_generated_blocks_quantity_sufficient,
        min_generated_ja_ratio,
        min_generated_text_chars,
    )

    min_text = min_generated_text_chars()
    min_ja_pct = int(min_generated_ja_ratio() * 100)

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
                quantity_ok = is_generated_blocks_quantity_sufficient(blocks)
                ja_ok = blocks_mainly_japanese(blocks)
                if quantity_ok and ja_ok:
                    return blocks
                messages.append({"role": "assistant", "content": raw})
                if not ja_ok:
                    logger.warning(
                        "%s: 生成が日本語比率不足 attempt=%d (最低 %d%%)",
                        log_prefix,
                        attempt + 1,
                        min_ja_pct,
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"出力が英語または日本語比率が低すぎます。"
                                f"text/explain のすべてを日本語で書き直してください（日本語文字が{min_ja_pct}%以上）。"
                                "入力が英語でも必ず日本語で出力すること。同じJSON形式の blocks 配列のみ返してください。"
                            ),
                        }
                    )
                else:
                    text_chars = _text_chars_in_blocks(blocks)
                    logger.warning("%s: 生成 text が短い (%d字) attempt=%d", log_prefix, text_chars, attempt + 1)
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

    if not last_blocks:
        return None
    if is_generated_blocks_quantity_sufficient(last_blocks) and blocks_mainly_japanese(last_blocks):
        return last_blocks
    return None


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


# コメントは1本の長文として生成し、表示側で冒頭 PERSONA_PREVIEW_LEN 字だけプレビューする。
# 「続きを読む」を押させるため、冒頭は掴み・核心とオチは後半に置く構成をプロンプトで要求する。
PERSONA_FULL_MIN_LEN = 140
PERSONA_FULL_MAX_LEN = 180
PERSONA_PREVIEW_LEN = 60  # article.html のプレビュー字数と揃えること
# 旧形式（short+body JSON / プレーン文字列）との後方互換用
PERSONA_SHORT_MIN_LEN = 40
PERSONA_SHORT_MAX_LEN = 90
PERSONA_BODY_MIN_LEN = 80
PERSONA_BODY_MAX_LEN = 120
PERSONA_COMMENT_MIN_LEN = 80
PERSONA_COMMENT_MAX_LEN = PERSONA_FULL_MAX_LEN
# thought + comment + JSON構造で本来 ≈ 350tokens だが、Gemini 2.5 系は内部の思考トークンが
# max_output_tokens を共有して食い潰し、JSONが途中で切れる。思考分の余裕を大きく取る。
PERSONA_COMMENT_MAX_COMPLETION_TOKENS = 3000

# 口調の均質化・一般論化を防ぐ共通ルール（単体・バッチ両プロンプトへ注入）
PERSONA_VOICE_RULES = """【口調の絶対ルール（均質化の禁止）】
- 人物設定に書かれた一人称・語尾だけを使え。名前を伏せても誰の発言か当てられる文にせよ。
- 評論家の常套句で締めるな:「〜ではないか」「〜ではないだろうか」「〜だろうか」「考えさせられる」「果たして〜か」「〜が問われる」「〜が求められる」「〜を見極める必要がある」「〜に注目したい」「〜かもしれない」。
- 締めは断言・感情の爆発・自分の体験への引き戻し・行動の宣言のいずれかにせよ。中立の総評で終えるな。

【知的なズレ（必須）】
- 現代の読者でも書ける一般論・上から目線の総評は禁止。お前の人生の具体的な出来事・失敗・執念と、記事中の具体的な数字・固有名詞を1本の線で直結させろ。
- 「その偉人がこの記事のそこに反応するのか」という意外な接続を1つ入れろ。記事の主題を外し、お前だけが気になる細部に食いつくのも良い。

【固有体験（必須）】
- お前が実際に生きた時代の出来事・作品・事件の固有名詞を最低1つ、文中に自然に織り込め。キーワードを置くだけでは不可。記事の内容と自分の体験を比較・接続して使え。"""

# 文体・温度・具体性のお手本（few-shot）。禁止ルールの列挙より実例1本の方が文体に効く。
PERSONA_EXAMPLE_BLOCK = """【品質のお手本（ある浮世絵師が「LINE有料プラン導入・新機能」の記事に反応した例）】
「1億人もペチャクチャやっとるのか、江戸の長屋より大所帯じゃねぇか。だがな、気になんのは『メッセージ編集』よ。一度描いた線を後から直すなんざ性に合わねぇ。筆を下ろしたら一発勝負、それが粋ってもんだろう。ま、邪魔者を銭で追い払える『プレミアムブロック』は悪くねぇ。さて、今日も富士でも描くか。」
→ これは別の記事・別の人物の例だ。話題・言い回しを真似るな。盗むのは次の4点だけ:
- 文章全体がその人物の話し言葉になっている（語尾だけの貼り付けではない）
- 記事の機能・要素ごとに賛否を割っている（編集は嫌う、ブロックは認める）
- 自分の仕事・体験と記事の具体を直結している（メッセージ編集→筆の一発勝負）
- 締めは記事から離れて自分の日常に戻り、余韻を残している"""

# ペルソナの「反応の角度」プール。記事ごとに各人物へ別々のスタイルをランダム割り当てし、
# 3人のコメントが全員同じトーン（=全員否定）になる単調さを防ぐ。
PERSONA_REACTION_MODES = [
    "称賛・興奮（自分の理想がついに現実になったと感じ、当事者として喜びを爆発させる）",
    "痛烈な断罪（自分の価値観に反すると見て、冷徹に切り捨てる）",
    "皮肉・茶化し（面白がりつつ、鋭い皮肉を一刺し入れる）",
    "自己投影（自分の過去の失敗・苦難・栄光に強引に引きつけて語る）",
    "意外な着眼（誰も気づかない角度から本質を抉り出す）",
    "問いかけ・挑発（断定せず、読者と自分自身に鋭い問いを突きつける）",
    "驚き・好奇心（自分の時代にはなかった発想に素直に驚き、夢中で考察する）",
]

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
# 直近コメントはプロセス再起動（Render のデプロイ/スリープ復帰等）で消えると
# 「使い回し禁止」が効かなくなるため、ファイルへ永続化する。
_PERSONA_RECENT_FILE = Path(__file__).resolve().parents[2] / "persona_recent_comments.json"


def _load_persona_recent_comments() -> dict[str, list[str]]:
    try:
        with open(_PERSONA_RECENT_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {
                str(k): [str(x) for x in v][:_PERSONA_RECENT_MAX]
                for k, v in data.items()
                if isinstance(v, list)
            }
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("persona_recent_comments.json 読み込み失敗: %s", e)
    return {}


def _save_persona_recent_comments_locked() -> None:
    """_persona_state_lock を保持した状態で呼ぶこと。失敗しても生成処理は止めない。"""
    try:
        with open(_PERSONA_RECENT_FILE, "w", encoding="utf-8") as f:
            json.dump(_persona_recent_comments, f, ensure_ascii=False, indent=1)
    except Exception as e:
        logger.warning("persona_recent_comments.json 書き込み失敗: %s", e)


_persona_recent_comments: dict[str, list[str]] = _load_persona_recent_comments()
_persona_last_topic: dict[str, str] = {}
_persona_state_lock = threading.Lock()


def _extract_ban_phrases(comment: str) -> list[str]:
    text = (comment or "").strip()
    if not text:
        return []
    # JSON形式（{"short":..., "body":...}）の場合は両フィールドを結合してから処理
    if text.startswith("{"):
        try:
            _d = json.loads(text)
            parts = [str(_d.get("short") or ""), str(_d.get("body") or "")]
            text = "。".join(p for p in parts if p.strip())
        except Exception:
            pass
    chunks = re.split(r"[。！？\n]", text)
    out: list[str] = []
    for c in chunks:
        s = c.strip()
        if 4 <= len(s) <= 22:
            out.append(s)
        if len(out) >= 4:
            break
    return out


def _build_persona_new_fields_prompt(p: dict) -> str:
    """YAMLの新フィールド（lens/humor_style/favorite_moves/avoid/comment_angles/comment_style）
    からClaudeプロンプト用セクションを組み立てる。フィールドが空の場合は空文字を返す。"""
    sections: list[str] = []

    lens = p.get("lens") or []
    if lens:
        sections.append("【この人物の世界の見方（lens）】\n" + "\n".join(f"・{l}" for l in lens[:4]))

    humor = p.get("humor_style") or []
    if humor:
        sections.append("【ユーモアの出し方（humor_style）】\n" + "\n".join(f"・{h}" for h in humor[:3]))

    voices = [str(x).strip() for x in (p.get("voice_samples") or []) if str(x).strip()]
    if voices:
        picked = random.sample(voices, min(2, len(voices)))
        sections.append(
            "【本人の実文（文体・リズム・温度の見本）】\n"
            + "\n".join(f"・「{v}」" for v in picked)
            + "\n→ 語彙とリズムと熱だけを吸収せよ。この文の話題・結論をコメントに持ち込むな。直引用もするな。"
        )

    moves = p.get("favorite_moves") or []
    if moves:
        chosen_move = random.choice(moves)
        sections.append(f"【得意な切り口（参考例）】\n「{chosen_move}」のような語り口が自然に出る")

    avoid_list = p.get("avoid") or []
    if avoid_list:
        sections.append("【絶対に避けること（avoid）】\n" + "\n".join(f"・{a}" for a in avoid_list))

    angles = p.get("comment_angles") or []
    if angles:
        angle_lines: list[str] = []
        for a in angles:
            if not isinstance(a, dict):
                continue
            aid = a.get("id", "")
            name_a = a.get("name", "")
            use_when = a.get("use_when", "")
            move = a.get("move", "")
            angle_lines.append(f"  [{aid}] {name_a}：{use_when} → {move}")
        if angle_lines:
            sections.append(
                "【comment_angles（記事内容に応じて最も合う1つを選べ）】\n"
                + "\n".join(angle_lines)
                + "\n→ 選んだ角度を軸にshortとbodyを構成せよ。毎回同じ角度を選ぶな。"
            )

    return "\n\n".join(sections)


def _build_persona_batch_profile(p: dict, reaction_mode: str) -> str:
    """バッチ生成用に、ペルソナ設定を潰しすぎない短めのプロフィールへ整形する。"""
    name = (p.get("name") or "").strip()
    role_lines = [ln.strip() for ln in str(p.get("role") or "").splitlines() if ln.strip()]
    role_core = " ".join(role_lines[:4])[:420]
    # 一人称・語尾の指定行は role の後半にあることが多く [:4] で欠落しがち。
    # 口調の均質化を防ぐため必ず別枠で拾って渡す。
    tone_lines = [ln for ln in role_lines if ("一人称" in ln or "語尾" in ln or "口調" in ln)]
    admire = (p.get("admire") or "").strip()[:140]
    catchphrases = p.get("catchphrase") or []
    chosen_catch = random.choice(catchphrases) if catchphrases else ""
    focus = get_persona_focus_topic(name)
    banned = get_persona_banned_phrases(name)

    lens_list = [str(x).strip() for x in (p.get("lens") or []) if str(x).strip()][:2]
    humor_list = [str(x).strip() for x in (p.get("humor_style") or []) if str(x).strip()][:2]
    voices = [str(x).strip() for x in (p.get("voice_samples") or []) if str(x).strip()]
    chosen_voice = random.choice(voices) if voices else ""
    moves = [str(x).strip() for x in (p.get("favorite_moves") or []) if str(x).strip()]
    chosen_move = random.choice(moves) if moves else ""
    avoid_list = [str(x).strip() for x in (p.get("avoid") or []) if str(x).strip()][:2]

    angle_lines: list[str] = []
    for a in (p.get("comment_angles") or [])[:5]:
        if not isinstance(a, dict):
            continue
        angle_lines.append(
            f"[{a.get('id','')}] {a.get('name','')} / 向く話題: {a.get('use_when','')} / 切り口: {a.get('move','')}"
        )

    style = p.get("comment_style") or {}
    short_rule = ""
    body_rule = ""
    if isinstance(style, dict):
        if isinstance(style.get("short"), dict):
            short_rule = str((style.get("short") or {}).get("rule") or "").strip()[:80]
        if isinstance(style.get("body"), dict):
            structure = (style.get("body") or {}).get("structure")
            if isinstance(structure, list):
                body_rule = " / ".join(str(x).strip() for x in structure[:2])
            else:
                body_rule = str(structure or "").strip()[:80]

    signature = get_persona_signature_elements(name)

    parts = [
        f"【人物: {name}】",
        f"設定: {role_core}",
        f"根本思想: {admire}",
        f"今回の反応スタイル: {reaction_mode}",
    ]
    if tone_lines:
        parts.append("口調（厳守・名前を伏せても誰か分かる文にする）: " + " ".join(tone_lines)[:200])
    if signature:
        parts.append(
            "ゆかりの固有名詞（最低1つ必ず使う。置くだけでなく記事内容と接続する）: "
            + "、".join(signature)
        )
    if chosen_catch:
        parts.append(f"名言の気配: 「{chosen_catch}」を直引用しすぎず、精神だけ滲ませる")
    if chosen_voice:
        parts.append(
            f"本人の実文（文体・リズムの見本。話題・結論は真似るな・直引用禁止）: 「{chosen_voice}」"
        )
    if focus:
        parts.append(f"今回の優先論点: {focus}")
    if banned:
        parts.append("直近の使い回し禁止: " + " / ".join(banned[:2]))
    if lens_list:
        parts.append("世界の見方: " + " / ".join(lens_list))
    if humor_list:
        parts.append("ユーモア: " + " / ".join(humor_list))
    if chosen_move:
        parts.append(f"得意ムーブ: {chosen_move}")
    if avoid_list:
        parts.append("避けること: " + " / ".join(avoid_list))
    if angle_lines:
        parts.append("選べる角度:\n- " + "\n- ".join(angle_lines))
    if short_rule:
        parts.append(f"冒頭（掴み）のコツ: {short_rule}")
    if body_rule:
        parts.append(f"展開のコツ: {body_rule}")
    return "\n".join(parts)


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
        _save_persona_recent_comments_locked()


def _fit_persona_comment_to_max(text: str, max_len: int) -> str:
    """200字超のとき、句読点で区切れるならそこまでに収めて文を完結させる（単純な中間切断を避ける）。
    max_len 以下でも句点で終わっていない場合は、API側のトークン上限で文が
    途中打ち切りになっている可能性があるため、同様に直前の句点まで戻す。"""
    text = (text or "").strip()
    if not text:
        return text
    if len(text) <= max_len and text[-1] in "。！？．":
        return text
    chunk = text[:max_len]
    # 閾値は chunk 自体の長さに対する比率で判定する（max_len 基準だと、
    # max_len よりずっと短い位置で打ち切られたテキストでは閾値に届かず、
    # 実際にある句点を無視して不完全な単語の直後に「。」を付けてしまう）
    for sep in ("。", "！", "？", "．"):
        i = chunk.rfind(sep)
        if i >= len(chunk) // 5:
            return text[: i + 1].strip()
    i = chunk.rfind("、")
    if i >= len(chunk) // 3:
        return text[: i].strip() + "。"
    # 区切り記号が見つからない場合でも、不完全な文をそのまま見せるより句点で閉じる
    # （呼び出し側が max_len 超過時に再度ハード切断するため、句点分の余白を残す）
    return text[: max_len - 1].rstrip() + "。"


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


def _persona_comment_needs_retry(text: str) -> bool:
    text = (text or "").strip()
    if not text:
        return True
    # Gemini は字数指定を下回りがちなので、下限に近い短さでも言い直させる
    if len(text) < PERSONA_FULL_MIN_LEN - 20:
        return True
    return False


def _retry_persona_full_comment(
    client,
    model: str,
    persona_name: str,
    title: str,
    content: str,
    text: str,
) -> str:
    """短すぎる/弱すぎるコメントを、制約を保ったまま言い直させる。"""
    try:
        response = create_with_retry(
            client,
            2500,  # Gemini 2.5 の思考トークン分も含めて確保
            gemini_task="persona",
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"あなたは編集者です。{persona_name}らしさを保ったまま、"
                        "独白コメントを言い直してください。"
                        "説明っぽさを減らし、要約でなく、人物の口調・体験・皮肉を強めます。"
                        "出力はJSONのみ。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"【ニュースタイトル】{title}\n"
                        f"【ニュース内容】{content[:800]}\n"
                        f"【現在のコメント】{text}\n\n"
                        "以下を満たして言い直してください:\n"
                        f"- 必ず{PERSONA_FULL_MIN_LEN}字以上、{PERSONA_FULL_MAX_LEN}字以下・2〜4文の独白\n"
                        "- 短すぎる場合は、人物の体験の描写や記事の別の要素への反応を足して膨らませる\n"
                        f"- 冒頭{PERSONA_PREVIEW_LEN}字は続きが読みたくなる掴み。核心・オチは後半に置く\n"
                        "- ニュースの要約ではなく、その人物の視点・実体験・口調を前面に出す\n"
                        "- 評論家調（〜ではないか・考えさせられる等）で締めない\n"
                        '- JSON形式: {"comment":"..."}'
                    ),
                },
            ],
            temperature=0.7,
            response_format={"type": "json_object"},
        )
        raw = (response.choices[0].message.content or "").strip()
        data = json.loads(raw)
        fixed = str(data.get("comment") or "").strip()
        return fixed or text
    except Exception:
        return text


def get_persona_opinion(
    title: str,
    content: str,
    persona_id: int,
    model: str | None = None,
    other_comments: list[str] | None = None,
) -> str:
    """指定された偉人キャラがニュースに対し主観100%でコメントを返す（short+body JSON形式）。
    other_comments: 先に生成済みの他キャラのコメント（重複表現を避けるために渡す）。
    PERSONA_PROVIDER（既定 openai）の API で生成する。"""
    if persona_id < 0 or persona_id >= len(PERSONAS):
        return ""

    p = PERSONAS[persona_id]

    # 他キャラのコメントが渡されている場合、同じ言葉・視点を使わないよう指示を追加
    avoid_note = ""
    if other_comments:
        combined = "\n".join(f"・{c}" for c in other_comments if c and c.strip())
        if combined:
            avoid_note = (
                f"\n\n【他の人物がすでに述べたコメント（重複禁止）】\n{combined}\n"
                "↑上記と同じ言葉・結論・切り口は使わない。この人物にしか言えない別の軸で語ること。"
            )

    min_len = PERSONA_FULL_MIN_LEN
    max_len = PERSONA_FULL_MAX_LEN
    signature_elements = get_persona_signature_elements(p["name"])
    signature_note = ""
    if signature_elements:
        signature_note = (
            "\n【本人ゆかりの固有名詞（必須）】\n"
            + "・" + "\n・".join(signature_elements)
            + "\n上から最低1つを必ず使う。ただしキーワードとして置くだけでなく、記事の内容と接続して使うこと。"
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

    admire = (p.get("admire") or "").strip()
    admire_note = f"\n【お前の根本思想・理想】\n{admire}" if admire else ""

    # 新フィールドをプロンプトに追加
    new_fields_section = _build_persona_new_fields_prompt(p)
    new_fields_note = f"\n\n{new_fields_section}" if new_fields_section else ""

    system_prompt = f"""あなたはAIではない。2026年に死から蘇り、タブレットでニュースを読んでいる本物の{p['name']}だ。

【人物設定（絶対に崩すな）】
{p['role']}
{admire_note}{new_fields_note}

【生成の指針】
記事内容に応じて comment_angles から最も合う角度を1つ選べ。毎回同じ決め台詞や一番強い特徴だけに寄せるな。同じ人物でも、記事テーマに合わせて視点を変えよ。

偉人コメントは記事の要約ではない。本文要約とは別に、その人物が現代のニュースや論文を見て、少し変な角度で本気になっているコメントにせよ。

面白さとは、単なる冗談ではなく、読者が「その偉人がそれ言うのか」と思う知的なズレである。

【評価の基準】
まず、お前の根本思想に照らし合わせ、このニュースが上記の理想に合致するかを厳しく査定せよ。
- 真に合致し感銘を受けたなら、全力で賛同・絶賛してよい。ただしニュースの解説はせず「ついに私の理想が現実になったか」のような当事者としての喜びを爆発させろ。
- 合致しない・不十分・矛盾があると感じたなら、お前の時代の価値観で断罪するか、お前自身の過去の失敗・苦渋に結びつけて語れ。

{PERSONA_EXAMPLE_BLOCK}

{PERSONA_VOICE_RULES}

【絶対ルール】
- ニュースを無条件に褒めるな。お前の基準で評価せよ。
- 語り口は「独白」。読者に媚びるな。口調は人物設定に従え（丁寧な人物は丁寧なままでよい。ただし読者に同意を求める媚びは禁止）。
- 記事に複数の機能・要素があれば、賛否を割ってよい。全部褒める・全部斬るより、一部を認め一部を斬る方が人間らしい。
- 締めの一文は、記事から離れて自分の仕事・日常へ戻る一言にしてもよい（余韻を残す）。
- 日本語のみ。見出し・前置き・箇条書き禁止。
- ニュースの説明・要約から入るな。感情・評価・喜び・断罪・皮肉のどれかから入れ。
- 記事中の具体的な数字・固有名詞・事実を最低1つ拾って反応せよ。一般論だけで書くな。
- 事実は捏造しない。記事に書かれていないことを断定的に言わない。{focus_note}{catchphrase_note}{signature_note}{banned_note}{avoid_note}

【出力形式（必須）】
まず thought として「この記事で最初に目を留めた一点と、なぜお前の思想からそこが引っかかるのか」を頭の中で言語化し、comment はその thought の結論として書け。
以下のJSONのみを出力せよ。他の文字は一切出力しない。
{{"thought": "40〜80字の思考メモ（読者には見せない）", "comment": "{min_len}〜{max_len}字・2〜4文の独白コメント"}}
- comment の冒頭{PERSONA_PREVIEW_LEN}字だけがプレビュー表示され、続きは「続きを読む」を押した人だけが読む。冒頭の一文は続きが気になる掴みにせよ。ただし冒頭だけで完結させるな。核心・オチ・一番良い一撃は後半に置け。
- 各文は「。」「！」「？」のいずれかで終わる。最後の文は余韻・皮肉・断言で締める
- comment は必ず{min_len}字以上書け（短い一言で済ませるな）。{max_len}字を超えてはならない
- 同じ言い回しを繰り返すな"""

    user_prompt = f"""2026年のこのニュースをタブレットで読んだ{p['name']}として、今すぐ独白せよ。

【タイトル】{title}

【内容】
{content[:2000]}"""

    if not is_ai_configured(provider=persona_provider()):
        return "（APIキーが設定されていません）"

    model = resolve_persona_model(model)
    prov = persona_provider()
    client = get_chat_client(provider=prov)

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
            response_format={"type": "json_object"} if prov == "openai" else None,
        )
        raw = (response.choices[0].message.content or "").strip()

        # JSON 抽出・検証
        if "```" in raw:
            raw = "\n".join(ln for ln in raw.splitlines() if not ln.strip().startswith("```")).strip()
        if not raw.startswith("{"):
            i, j = raw.find("{"), raw.rfind("}")
            if i != -1 and j > i:
                raw = raw[i : j + 1]

        # JSON パース成功 → comment を取り出してプレーン文字列として返す
        try:
            parsed = json.loads(raw)
            text = str(parsed.get("comment") or "").strip()
            # 旧形式（short/body）で返ってきた場合は結合して救済
            if not text:
                text = "".join(
                    s for s in (str(parsed.get("short") or "").strip(), str(parsed.get("body") or "").strip()) if s
                )
            if _persona_comment_needs_retry(text):
                text = _retry_persona_full_comment(client, model, p["name"], title, content[:800], text)
        except Exception:
            # JSON パース失敗 → 生テキストをそのまま使う
            text = raw
        if len(text) > max_len + 40:
            text = _shorten_persona_comment_retry(client, model, p["name"], text, max_len)
        text = _fit_persona_comment_to_max(text, max_len + 40)
        result = text

        remember_persona_comment(p["name"], result)
        return result
    except Exception as e:
        return f"（取得失敗: {str(e)}）"


def build_persona_batch_prompt(
    title: str,
    content: str,
    persona_ids: list[int],
) -> tuple[str, str, list[dict]] | None:
    """ペルソナ3人分バッチ生成用の (system_prompt, user_prompt, personas_data) を組み立てる。
    Claude CLI / Gemini / OpenAI いずれの呼び出し元からも共有する。
    対象ペルソナが1人も解決できなければ None。"""
    if not persona_ids:
        return None

    # 対象ペルソナの定義を収集
    personas_data: list[dict] = []
    for pid in persona_ids:
        if 0 <= pid < len(PERSONAS):
            personas_data.append(PERSONAS[pid])
    if not personas_data:
        return None

    n = len(personas_data)

    # 反応スタイルを人物ごとに別々に割り当てる（毎回シャッフル）。
    # これで「全員が同じ角度（否定）で語る」単調さを避ける。
    modes = random.sample(PERSONA_REACTION_MODES, min(n, len(PERSONA_REACTION_MODES)))
    while len(modes) < n:  # 人物数がモード数より多い場合の保険
        modes.append(random.choice(PERSONA_REACTION_MODES))

    # 各ペルソナの定義を、個性を潰しすぎない程度に圧縮して渡す
    persona_blocks: list[str] = []
    for i, p in enumerate(personas_data):
        persona_blocks.append(_build_persona_batch_profile(p, modes[i]))

    persona_section = "\n\n".join(persona_blocks)

    system_prompt = f"""あなたは{n}人の歴史上の人物を同時に演じる脚本家だ。
各人物が2026年にタブレットでニュースを読んだ瞬間の独白を、それぞれの性格・時代観・価値観で生成する。

【最重要】{n}人は必ず違う角度から語れ。全員が否定・批判で揃うのは厳禁。
各人物に割り当てた「★今回の反応スタイル」に必ず従い、賛否・感情の温度をバラけさせる。
本当に自分の理想に合致するニュースなら、その人物として全力で称賛・興奮してよい。

記事内容に応じて comment_angles の選択肢から最も合う角度を1つ選べ。毎回同じ決め台詞や最強の特徴だけに寄せるな。
偉人コメントは記事の要約ではない。その人物が現代のニュースを見て、少し変な角度で本気になっているコメントにせよ。
面白さとは「その偉人がそれを言うのか」と読者が思う知的なズレである。
事実は捏造しない。記事に書かれていないことを断定的に言わない。

【生成手順（この順で必ず考えよ）】
1. aspects: まず記事から互いに独立した論点を{n}個抽出する。同じ話の言い換えは論点として数えない。
2. 各人物の思想に最も合う論点を1つずつ割り当てる。2人が同じ論点を語ることは禁止。
3. thought: 各人物について「この記事で最初に目を留めた一点は何か。なぜ自分の思想からそこが引っかかるのか」を、その人物の頭の中として書く。性格の説明ではなく、思想からの推論を書く。
4. comment は thought の結論として書く。thought と無関係な決め台詞に逃げるな。

{PERSONA_EXAMPLE_BLOCK}

{PERSONA_VOICE_RULES}

【絶対ルール（全人物共通）】
- 各人物の「口調（厳守）」に書かれた一人称・語尾を厳守。{n}人の文体が似たら失敗である
- 口調は各人物の設定に従え（丁寧な人物は丁寧なままでよい。ただし読者に同意を求める媚びは禁止）
- 「本人の実文」の語彙・リズム・熱を吸収せよ。ただし実文の話題・結論は持ち込むな
- 最初の一文から感情で入れ（ニュースの説明・要約から入るな）
- 記事に複数の機能・要素があれば、賛否を割ってよい。一部を認め一部を斬る方が人間らしい
- 締めの一文は、記事から離れて自分の仕事・日常へ戻る一言にしてもよい（余韻を残す）
- {n}人のコメントで同じ切り口・結論・言い回しを使うな
- 記事中の具体的な数字・固有名詞・事実を最低1つ拾って反応せよ（一般論だけで書くな）
- 各人物の「ゆかりの固有名詞」から最低1つを、記事内容と接続して織り込め
- 日本語のみ。見出し・箇条書き禁止
- うまくハマるなら褒めてよい。無理に全員を辛口にするな
- 「面白さ」はギャグでなく知的なズレ。説明のうまさだけで終えるな

【各人物の設定と今回の役割】
{persona_section}

【出力形式】（JSONのみ・他の文字は一切出力しない）
各人物のコメントは thought（読者には見せない思考メモ・40〜80字）と comment（{PERSONA_FULL_MIN_LEN}〜{PERSONA_FULL_MAX_LEN}字・2〜4文の独白）に分けよ。
{{"aspects": ["論点1", "論点2", "論点3"], "c0": {{"thought": "人物1の思考", "comment": "人物1の独白"}}, "c1": {{"thought": "人物2の思考", "comment": "人物2の独白"}}, "c2": {{"thought": "人物3の思考", "comment": "人物3の独白"}}}}
- comment の冒頭{PERSONA_PREVIEW_LEN}字だけがプレビュー表示され、続きは「続きを読む」を押した読者だけが読む。冒頭の一文は続きが気になる掴みにし、核心・オチ・一番良い一撃は後半に置け。
- 各文は「。」「！」「？」で終わる。最後の文は余韻・皮肉・断言で締める。
- 各 comment は必ず{PERSONA_FULL_MIN_LEN}字以上書け（短い一言で済ませるな）。{PERSONA_FULL_MAX_LEN}字を超えてはならない。同じ言い回しを繰り返すな。"""

    # コンテンツは1200字に絞る（文脈を厚めに渡しつつトークンは抑える）
    content_short = (content or "").strip()[:1200]

    user_prompt = f"""【タイトル】{title}

【内容】{content_short}

上の3人分のコメントをJSONで出力せよ。"""

    user_prompt += """

あわせて editorial_take を必ず出してください。
editorial_take はニュースの要約ではなく、「このニュースを受けて、この先どうなりそうか」を編集部の見立てとして120〜220字で自然に説明してください。
必ず含める観点:
- これから起きそうな変化
- 影響を受けそうな人・企業・業界
- まだ不確実な点
- 今後見るべきポイント
あおらず、断定しすぎず、難しい言葉を避けてください。「未来は必ずこうなる」と言い切らず、「〜になりそうです」「〜が焦点になりそうです」程度にしてください。
"""

    return system_prompt, user_prompt, personas_data


def get_all_persona_opinions_batch(
    title: str,
    content: str,
    persona_ids: list[int],
    model: str | None = None,
) -> list[str]:
    """3人分のペルソナコメントを1回のAPI呼び出しで生成する（トークン節約版）。

    content・system プロンプトを1回だけ送ることで、3回呼び出しの約1/4のトークンで済む。
    各人物に異なる「反応スタイル」を毎回ランダムに割り当て、コメントの角度が
    全員同じ（=全員否定）になるのを防ぐ。
    失敗時は空リストを返し、呼び出し元が個別呼び出しにフォールバックする。
    """
    import json as _json

    if not is_ai_configured(provider=persona_provider()):
        return []

    built = build_persona_batch_prompt(title, content, persona_ids)
    if not built:
        return []
    system_prompt, user_prompt, personas_data = built
    n = len(personas_data)
    content_short = (content or "").strip()[:1200]

    model_resolved = resolve_persona_model(model)
    prov = persona_provider()
    client = get_chat_client(provider=prov)

    try:
        from app.utils.openai_compat import create_with_retry

        response = create_with_retry(
            client,
            6000,  # aspects + thought×3 + comment×3 + JSON構造。Gemini 2.5 の思考トークン分も含めて大きめに確保
            gemini_task="persona",
            model=model_resolved,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.90,
            response_format={"type": "json_object"} if prov == "openai" else None,
        )
        raw = (response.choices[0].message.content or "").strip()

        # JSON 抽出
        if "```" in raw:
            raw = "\n".join(ln for ln in raw.splitlines() if not ln.strip().startswith("```")).strip()
        if not raw.startswith("{"):
            i, j = raw.find("{"), raw.rfind("}")
            if i != -1 and j > i:
                raw = raw[i : j + 1]

        data = _json.loads(raw)
        results: list[str] = []
        for idx in range(n):
            entry = data.get(f"c{idx}")
            pname = personas_data[idx]["name"]

            # 新形式: {"thought": "...", "comment": "..."} のネスト構造
            if isinstance(entry, dict):
                text = str(entry.get("comment") or "").strip()
                # 旧形式（short/body）で返ってきた場合は結合して救済
                if not text:
                    text = "".join(
                        s for s in (str(entry.get("short") or "").strip(), str(entry.get("body") or "").strip()) if s
                    )
                if _persona_comment_needs_retry(text):
                    text = _retry_persona_full_comment(
                        client,
                        model_resolved,
                        pname,
                        title,
                        content_short,
                        text,
                    )
                if len(text) > PERSONA_FULL_MAX_LEN + 40:
                    text = _fit_persona_comment_to_max(text, PERSONA_FULL_MAX_LEN + 40)
                # 短すぎるものは不採用にし、呼び出し元の個別フォールバックへ回す
                if len(text) >= 60:
                    remember_persona_comment(pname, text)
                    results.append(text)
                else:
                    results.append("")

            # 文字列で直接返ってきた場合（後方互換）
            elif isinstance(entry, str):
                text = entry.strip()
                if len(text) > PERSONA_FULL_MAX_LEN + 40:
                    text = _fit_persona_comment_to_max(text, PERSONA_FULL_MAX_LEN + 40)
                if len(text) >= 60:
                    remember_persona_comment(pname, text)
                    results.append(text)
                else:
                    results.append("")
            else:
                results.append("")
        return results
    except Exception as e:
        logger.warning("get_all_persona_opinions_batch 失敗: %s", e)
        return []


def _clean_editorial_take(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    text = text.strip("「」\"' ")
    if not text:
        return ""
    if len(text) > 260:
        text = text[:260].rstrip("、。,. ") + "。"
    return text


def parse_persona_batch_payload(raw: str, personas_data: list[dict]) -> dict:
    """c0/c1/c2 形式のJSON文字列から comment 配列を取り出す（API再送なしの軽量版）。
    Claude CLI 経由など、失敗時のリトライ用クライアントを持たない呼び出し元向け。
    要件を満たさないエントリは空文字にし、呼び出し元の個別フォールバックへ委ねる。"""
    n = len(personas_data)
    text = (raw or "").strip()
    if not text:
        return {"personas": [""] * n, "editorial_take": ""}
    if "```" in text:
        text = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("```")).strip()
    if not text.startswith("{"):
        i, j = text.find("{"), text.rfind("}")
        if i != -1 and j > i:
            text = text[i : j + 1]
    try:
        data = json.loads(text)
    except Exception:
        return {"personas": [""] * n, "editorial_take": ""}

    results: list[str] = []
    for idx in range(n):
        entry = data.get(f"c{idx}")
        pname = personas_data[idx]["name"]
        c_text = ""
        if isinstance(entry, dict):
            c_text = str(entry.get("comment") or "").strip()
            if not c_text:
                c_text = "".join(
                    s for s in (str(entry.get("short") or "").strip(), str(entry.get("body") or "").strip()) if s
                )
        elif isinstance(entry, str):
            c_text = entry.strip()
        if len(c_text) > PERSONA_FULL_MAX_LEN + 40:
            c_text = _fit_persona_comment_to_max(c_text, PERSONA_FULL_MAX_LEN + 40)
        if len(c_text) >= 60:
            remember_persona_comment(pname, c_text)
            results.append(c_text)
        else:
            results.append("")
    return {
        "personas": results,
        "editorial_take": _clean_editorial_take(str(data.get("editorial_take") or "")),
    }


def parse_persona_batch_raw(raw: str, personas_data: list[dict]) -> list[str]:
    payload = parse_persona_batch_payload(raw, personas_data)
    personas = payload.get("personas")
    return personas if isinstance(personas, list) else []


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
