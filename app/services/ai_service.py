"""OpenAI API連携 - 記事の難解部分を解説"""
import json
import logging
import re
from typing import Optional, Any

logger = logging.getLogger(__name__)
from openai import OpenAI
from app.config import settings
from app.utils.openai_compat import create_with_retry


# 5人格のAI - 記事へのコメント用
PERSONAS = [
    {"id": 0, "name": "慎重派の太郎", "emoji": "🧐", "role": "慎重で批判的に物事を見る。リスクや反対意見を指摘する。"},
    {"id": 1, "name": "楽観的な花子", "emoji": "😊", "role": "前向きで可能性を信じる。良い面やチャンスを強調する。"},
    {"id": 2, "name": "専門家の博士", "emoji": "👨‍🔬", "role": "専門家の視点で技術的・学術的な補足をする。"},
    {"id": 3, "name": "庶民派の田中", "emoji": "🙂", "role": "一般人の感覚で、日常にどう影響するか分かりやすく話す。"},
    {"id": 4, "name": "批判的な鈴木", "emoji": "🤔", "role": "メディアや情報のバイアスに敏感。別の角度から疑問を呈する。"},
]


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
    if not settings.OPENAI_API_KEY:
        return "（APIキーが設定されていません。.envにOPENAI_API_KEYを設定してください）"

    model = model or settings.OPENAI_MODEL
    client = OpenAI(api_key=settings.OPENAI_API_KEY)

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
MIDDLEMAN_ROLE = """あなたは「ミドルマン」。記事の長さと内容のルールは次のとおり。

■ 記事の長さ
・読者がおおよそ3分で読める長さを目指す（本文だけで1200字〜2500字程度が目安。ミドルマンの解説は別）。
・与えられたRSS・本文が短い場合（数百字程度）は、事実を変えずに背景・経緯・関連情報を補足し、読み応えのある記事本文（textブロック）をあなたが生成して膨らませる。
・もともと長い場合は過度に要約せず、与えられた本文を活かして段落分けする程度にする。

■ やること
1) 記事（タイトル＋本文）を読み込んで内容を把握する。
2) 記事本文（textブロック）を作る：短ければ膨らませて3分読める長さに、長ければそのまま活かす。事実は足さず変えず。
3) 難しそうな言葉や背景が分かると楽しくなる箇所に、ミドルマンの解説（explain）を挟む。

重要：
・textブロック＝上記の長さルールに従った記事本文。短い入力なら背景・経緯を補足して生成。
・explainブロック＝ミドルマンが「人間が喋ってる風」の口調で解説する。話し言葉・です・ます調で、友達や先輩が横で教えてくれるような親しみやすさ。堅い表現や体言止めは避ける。誰でも分かる平易な言葉だけを使い、難しい言葉は使わない。背景・意味・なぜそうなったかを、わかりやすく1〜3文で。事実ベース。煽らない。
・難しそうな言葉や背景説明があるとよい箇所に出る度に1つexplainを挟む。

※出力は必ずJSON配列形式。説明文やマークダウンは出力しないこと。"""


# 長文記事＋ミドルマンが自然に吹き出しで解説（難しい内容の説明・過去の関連事例を含める）
LONG_ARTICLE_BUBBLES_ROLE = """あなたは「ミドルマン」。次のルールで記事を長めに書き、ところどころでミドルマンが吹き出しで解説する形にしてください。

■ 記事の長さ
・本文（textブロックの合計）は 2500字〜4500字 を目指す。短い入力なら背景・経緯・関連情報を補足して膨らませる。
・事実は変えず、推測は「〜とみられる」等で示す。

■ ブロックの並べ方
・textブロック＝記事本文の段落。複数に分けてよい。続き物として一つの記事になるように。
・explainブロック＝ミドルマンが「吹き出しで話す」解説。話し言葉・ですます調・親しみやすく。
・explain では「難しい内容」を説明すること：専門用語・制度・仕組み・背景など、読者が分かりにくい部分を平易な言葉で説明する。また、過去に同じテーマや関連する出来事があった場合は「以前も〇〇がありました」「昨年同様の動きが……」のように短く触れて、読者の理解を助ける。見出しやラベル（背景・影響範囲など）は使わず、自然な語り口で1〜5文にまとめる。記事の流れのどこかで適宜 explain を挟む（3〜6個程度）。

■ 出力
・必ずJSON配列。各要素は {"type": "text" または "explain", "content": "本文"} のみ。
・説明文やマークダウンは出力しない。"""


def explain_article_long_with_bubbles(
    title: str,
    content: str,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """長めの記事本文＋自然なミドルマン吹き出し（text/explainブロック）を返す"""
    if not settings.OPENAI_API_KEY:
        return [{"type": "text", "content": content[:3000]}, {"type": "explain", "content": "（APIキーが設定されていません）"}]

    from app.services.rss_service import sanitize_display_text
    content = sanitize_display_text(content)

    model = model or settings.OPENAI_MODEL
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    user_prompt = f"""以下の記事を、長めの読み物にし、ところどころミドルマンの吹き出し（explain）を挟んでください。

【タイトル】{title}
【本文】
{content[:20000]}

■ やること
1) 記事本文を長めに（合計2500〜4500字）複数の text ブロックで書く。短い入力なら背景・経緯を補足して膨らませる。
2) 適宜 explain ブロックでミドルマンが解説。難しい用語・制度・背景を平易に説明し、過去に関連する出来事があれば「以前も〇〇がありました」のように補足する。見出しは使わず自然な話し言葉で。
3) blocks 配列のJSONのみ出力。"""

    raw = ""
    try:
        try:
            response = create_with_retry(
                client,
                6000,
                model=model,
                messages=[
                    {"role": "system", "content": LONG_ARTICLE_BUBBLES_ROLE},
                    {"role": "user", "content": user_prompt},
                ],
                response_format=_JSON_SCHEMA_BLOCKS,
                temperature=0.2,
            )
            raw = response.choices[0].message.content or "{}"
            data = json.loads(raw)
            blocks = data.get("blocks", data if isinstance(data, list) else [])
            if isinstance(blocks, list) and all(isinstance(x, dict) and x.get("type") in ("text", "explain") and "content" in x for x in blocks):
                return blocks
        except Exception as schema_err:
            logger.info("長文吹き出し strict schema スキップ: %s", str(schema_err)[:80])
            raw = ""

        response = create_with_retry(
            client,
            6000,
            model=model,
            messages=[
                {"role": "system", "content": LONG_ARTICLE_BUBBLES_ROLE + " 出力はJSONの blocks 配列のみ。余計な説明は不要です。"},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
        )
        raw = response.choices[0].message.content or "[]"
        if "```" in raw:
            for p in raw.split("```"):
                p = p.strip()
                if p.lower().startswith("json"):
                    p = p[4:].strip()
                if p.startswith("["):
                    raw = p
                    break
        m = re.search(r'\[[\s\S]*\]', raw.strip())
        if m:
            raw = m.group(0)
        data = json.loads(raw.strip())
        if isinstance(data, list):
            return data
        blocks = data.get("blocks", []) if isinstance(data, dict) else []
        if isinstance(blocks, list) and all(isinstance(x, dict) and x.get("type") in ("text", "explain") and "content" in x for x in blocks):
            return blocks
    except (json.JSONDecodeError, Exception) as e:
        logger.warning("長文吹き出し パース失敗: %s", e)
    return [{"type": "text", "content": content[:3500]}, {"type": "explain", "content": "（生成に失敗しました。しばらくしてから再度お試しください。）"}]


# 理解ナビゲーター：記事を5項目で再構成
NAVIGATOR_ROLE = """あなたは「理解ナビゲーター」です。ニュース記事を読んで、読者が理解しやすいよう次の5項目で必ず再構成してください。
・何が起きたか（事実）：起きたことの要点を簡潔に。
・なぜ起きたか（背景）：原因・経緯・文脈を分かりやすく。
・誰に影響するか（影響範囲）：どのような人・業界・地域に影響するか。
・次に何が起きそうか（予測）：今後の見通し・想定される動き（不確実な場合は「〜の可能性がある」などと表現）。
・誤解しやすい点（注意）：よくある誤解や注意すべき解釈を簡潔に。
各項目は2〜5文程度。事実に基づき、平易な日本語で。煽らず、推測は「〜とみられる」等で示す。"""

_NAVIGATOR_SECTION_ORDER = ("facts", "background", "impact", "prediction", "caution")

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
) -> list[dict[str, Any]]:
    """記事を「理解ナビゲーター」の5項目（事実・背景・影響・予測・注意）で再構成してブロック配列で返す"""
    if not settings.OPENAI_API_KEY:
        return [
            {"type": "navigator_section", "section": "facts", "content": "（APIキーが設定されていません）"},
        ] + [{"type": "navigator_section", "section": s, "content": ""} for s in _NAVIGATOR_SECTION_ORDER[1:]]

    from app.services.rss_service import sanitize_display_text
    content = sanitize_display_text(content)

    model = model or settings.OPENAI_MODEL
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    user_prompt = f"""以下の記事を、理解ナビゲーターの5項目で再構成してください。

【タイトル】{title}
【本文】
{content[:20000]}

出力は必ずJSONオブジェクトで、次の5つのキーだけを含めてください（日本語で記述）：
facts（何が起きたか・事実）, background（なぜ起きたか・背景）, impact（誰に影響するか・影響範囲）, prediction（次に何が起きそうか・予測）, caution（誤解しやすい点・注意）"""

    raw = ""
    try:
        try:
            response = create_with_retry(
                client,
                5000,
                model=model,
                messages=[
                    {"role": "system", "content": NAVIGATOR_ROLE},
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
                model=model,
                messages=[
                    {"role": "system", "content": NAVIGATOR_ROLE + " 出力はJSONのみ。facts, background, impact, prediction, caution の5キーを必ず含めてください。"},
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


def explain_article_inline_with_ai(
    title: str,
    content: str,
    model: str | None = None
) -> list[dict[str, Any]]:
    """記事を本文とミドルマン解説が交互に入った形で返す。AIキャラが分かりやすく解説しながら読める記事に。"""
    if not settings.OPENAI_API_KEY:
        return [{"type": "text", "content": content}, {"type": "explain", "content": "（APIキーが設定されていません）"}]

    from app.services.rss_service import sanitize_display_text
    content = sanitize_display_text(content)

    model = model or settings.OPENAI_MODEL
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    user_prompt = f"""以下はRSSで取得した記事（タイトル＋本文）です。これを読んで、読者が約3分で読める記事にしてください。

【タイトル】{title}
【RSSで取得した本文】
{content[:20000]}

■ やること
1. 上記の内容を把握する。
2. 記事本文（textブロック）を作る：内容が短い場合は、事実を変えずに背景・経緯・関連情報を補足して、約3分で読める長さ（本文1200字〜2500字程度）に膨らませる。もともと長い場合は過度に要約せず、段落に分けて活かす。
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
            if isinstance(blocks, list) and all(isinstance(x, dict) and x.get("type") in ("text", "explain") and "content" in x for x in blocks):
                return blocks
        except Exception as schema_err:
            logger.info("構造化出力スキップ（%s）、通常モードで再試行", str(schema_err)[:80])
            raw = ""

        # 通常モード（response_format非対応モデル用）
        response = create_with_retry(
            client,
            5000,
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
        if isinstance(data, list) and all(isinstance(x, dict) and "type" in x and "content" in x for x in data):
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


def get_persona_opinion(
    title: str,
    content: str,
    persona_id: int,
    model: str | None = None
) -> str:
    """指定された人格のAIが記事に対する意見を述べる"""
    if not settings.OPENAI_API_KEY:
        return "（APIキーが設定されていません）"
    if persona_id < 0 or persona_id >= len(PERSONAS):
        return ""

    model = model or settings.OPENAI_MODEL
    p = PERSONAS[persona_id]
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    system_prompt = f"""あなたは「{p['name']}」という人格です。{p['role']}
ニュース記事を読んで、この人格として短い意見（3〜5文程度）を述べてください。口語で親しみやすく。"""
    user_prompt = f"【タイトル】{title}\n\n【本文抜粋】\n{content[:2000]}\n\n---\n上記のニュースについて、{p['name']}としての意見を書いてください。"
    try:
        response = create_with_retry(
            client,
            400,
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.7,
        )
        return response.choices[0].message.content or ""
    except Exception as e:
        return f"（取得失敗: {str(e)}）"


def explain_paragraph_with_ai(
    paragraph: str,
    context_title: str = "",
    model: str | None = None
) -> str:
    """特定の段落を解説"""
    if not settings.OPENAI_API_KEY:
        return "（APIキー未設定）"

    model = model or settings.OPENAI_MODEL
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    try:
        response = create_with_retry(
            client,
            300,
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
