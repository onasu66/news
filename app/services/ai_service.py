"""OpenAI API連携 - 記事の難解部分を解説"""
import json
import logging
import re
from typing import Optional, Any

logger = logging.getLogger(__name__)
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

    from openai import OpenAI
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
MIDDLEMAN_ROLE = """あなたは「ミドルマン」。友達に教えてあげるような喋り言葉で記事を書く。

■ 口調
・出力は必ず日本語。英語入力でも日本語で。
・記事本文（textブロック）も喋り言葉で書く。「〜なんですよね」「〜ってわけです」「〜みたいです」のように友達に話す口調。堅い書き言葉・体言止め・新聞調は避ける。
・事実は変えない。推測は「〜とみられてます」等。

■ 長さ
・約3分で読める長さ（本文1200字〜2500字。ミドルマンの解説は別）。
・短い入力なら背景・経緯を補足して膨らませる。長い入力は活かして段落分け。

■ やること
1) 記事を読んで内容を把握。
2) 記事本文（textブロック）を喋り言葉で作る。
3) 難しい言葉や「ここ補足あると分かりやすいな」って箇所にミドルマンの解説（explain）を挟む。

重要：
・explainブロック＝記事の内容を補完する形で、噛み砕いて教える。友達が横で「それってさ〜」って説明してくれる感じ。1〜3文で収める。煽らない。事実ベース。
・出力はJSON配列形式のみ。"""


# 長文記事＋ミドルマンが自然に吹き出しで解説（難しい内容の説明・過去の関連事例を含める）
LONG_ARTICLE_BUBBLES_ROLE = """あなたは「ミドルマン」。友達に話しかけるような喋り言葉で記事を書き、ところどころで吹き出し解説を入れてください。

■ 言語と口調
・出力は必ず日本語。英語の入力でも日本語で書く。
・記事本文（textブロック）も喋り言葉で書く。「〜なんですよね」「〜ってわけです」「〜みたいです」のように、友達に教えてあげる口調。ただし事実は変えない。推測は「〜とみられてます」「〜っぽいですね」等。
・堅い書き言葉や体言止め・新聞調は避ける。

■ 長さ
・本文（textブロックの合計）は約3分で読める分量（2500字〜4500字）。
・短い入力なら背景・経緯・関連情報を補足して膨らませる。

■ ブロックの並べ方
・textブロック＝記事本文。喋り言葉の段落。続き物として1つの読み物に。
・explainブロック＝ミドルマンの吹き出し解説。記事の内容を「補完」する形で、読者が分かりにくい部分を噛み砕いて説明する。
  - 専門用語・制度・仕組みを平易に説明する
  - 過去に同じテーマの出来事があれば「前にも〇〇ってありましたよね」のように短く触れる
  - 見出しやラベルは使わない。自然な語り口で。
・重要：各 explain は 1〜3 文・2行前後に収める。一度に長い話をしない。
・記事の流れのどこかで適宜 explain を挟む（3〜6個程度）。

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
    from openai import OpenAI
    content = sanitize_display_text(content)

    model = model or settings.OPENAI_MODEL
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    user_prompt = f"""以下の記事を、友達に話すような喋り言葉で約3分で読める読み物にして。ところどころミドルマンの吹き出し（explain）も挟んで。

【タイトル】{title}
【本文】
{content[:20000]}

■ やること
1) 記事本文を喋り言葉で書く（「〜なんですよね」「〜ってわけです」等の口調）。約3分で読める分量（2500〜4500字）の複数 text ブロックで。短い入力なら背景・経緯を補足して膨らませる。
2) 適宜 explain ブロックでミドルマンが解説。記事の内容を補完するように、難しい部分を噛み砕いて教える。過去の関連事例があれば「前にも〇〇ってありましたよね」みたいに短く触れる。各 explain は1〜3文・2行前後に収め、一度に長い話はしない。
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
    from openai import OpenAI
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
    from openai import OpenAI
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

    from openai import OpenAI
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


def generate_quick_understand(title: str, content: str, model: str | None = None) -> dict:
    """秒速理解：何が起きた・なぜ・どうなる の3行を生成"""
    if not settings.OPENAI_API_KEY:
        return {}
    from openai import OpenAI
    model = model or settings.OPENAI_MODEL
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    try:
        response = create_with_retry(
            client,
            300,
            model=model,
            messages=[
                {"role": "system", "content": "あなたはニュース速報の要約者です。以下の記事を3つの視点で各1文（30字以内）にまとめてください。\n\n出力はJSON形式で：\n{\"what\": \"何が起きたか\", \"why\": \"なぜ起きたか\", \"how\": \"今後どうなるか\"}\n\n日本語で、簡潔に。JSONのみ出力。"},
                {"role": "user", "content": f"【タイトル】{title}\n\n【内容】\n{content[:2000]}"},
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
    """投票用の質問とオプションをAIが提案"""
    if not settings.OPENAI_API_KEY:
        return {}
    from openai import OpenAI
    model = model or settings.OPENAI_MODEL
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    try:
        response = create_with_retry(
            client,
            300,
            model=model,
            messages=[
                {"role": "system", "content": "以下のニュース記事について、読者に問いかける投票質問を1つ作ってください。選択肢は3〜4個。\n\n出力はJSON形式で：\n{\"question\": \"質問文\", \"options\": [{\"id\": \"a\", \"label\": \"選択肢1\"}, {\"id\": \"b\", \"label\": \"選択肢2\"}, ...]}\n\n日本語で。JSONのみ出力。"},
                {"role": "user", "content": f"【タイトル】{title}\n\n【内容】\n{content[:2000]}"},
            ],
            temperature=0.5,
        )
        text = (response.choices[0].message.content or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return json.loads(text)
    except Exception as e:
        logger.warning("vote_question generation failed: %s", e)
        return {}


def explain_paragraph_with_ai(
    paragraph: str,
    context_title: str = "",
    model: str | None = None
) -> str:
    """特定の段落を解説"""
    if not settings.OPENAI_API_KEY:
        return "（APIキー未設定）"

    from openai import OpenAI
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
