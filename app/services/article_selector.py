"""OpenAI による記事候補の一括選定（キュレーション）

RSS の軽量フィルタを通過した候補を OpenAI に一括送信し、
「掲載すべき記事か」をトレンド情報も踏まえて評価・スコア付けして返す。

- Google 検索急上昇 + X(Twitter) トレンドをプロンプトに含める
- API 呼び出しは候補全体で 1 回（コスト効率が良い）
- 失敗時は candidates をそのまま返す（安全なフォールバック）
- 環境変数 AI_CURATION_ENABLED=true で有効化
"""
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def is_ai_curation_enabled() -> bool:
    try:
        from app.config import settings
        return str(getattr(settings, "AI_CURATION_ENABLED", "false")).lower() in ("1", "true", "yes")
    except Exception:
        return False


def _fetch_all_trends(extra_keywords: list[str] | None = None) -> list[str]:
    """Google 急上昇 + X(Twitter) トレンドをまとめて取得してキーワードリストを返す。
    失敗しても空リストを返すだけで処理を止めない。"""
    seen: set[str] = set()
    keywords: list[str] = []

    # 呼び出し元から渡されたキーワードを最優先で追加
    for kw in (extra_keywords or []):
        k = kw.strip()
        if k and k.lower() not in seen:
            seen.add(k.lower())
            keywords.append(k)

    # Google 検索急上昇
    try:
        from app.services.trends_service import fetch_google_trends
        for item in fetch_google_trends():
            k = item.keyword.strip()
            if k and k.lower() not in seen:
                seen.add(k.lower())
                keywords.append(k)
    except Exception as e:
        logger.debug("Google トレンド取得スキップ: %s", e)

    # RapidAPI Super Duper Trends（設定時のみ）
    try:
        from app.services.trends_service import fetch_super_duper_trends
        for item in fetch_super_duper_trends():
            k = item.keyword.strip()
            if k and k.lower() not in seen:
                seen.add(k.lower())
                keywords.append(k)
    except Exception as e:
        logger.debug("Super Duper Trends 取得スキップ: %s", e)

    # X(Twitter) 急上昇
    try:
        from app.services.twitter_trends_service import fetch_twitter_trends
        for item in fetch_twitter_trends():
            k = item.keyword.strip()
            if k and k.lower() not in seen:
                seen.add(k.lower())
                keywords.append(k)
    except Exception as e:
        logger.debug("X トレンド取得スキップ: %s", e)

    return keywords[:40]  # 多すぎるとプロンプトが膨らむので上限を設ける


def select_articles_with_ai(
    candidates: list,
    max_select: int = 30,
    model: Optional[str] = None,
    trend_keywords: list[str] | None = None,
) -> list:
    """
    候補記事を OpenAI で一括評価し、掲載優先度が高い順に最大 max_select 件を返す。

    candidates      : NewsItem のリスト（軽量フィルタ通過済み）
    max_select      : 返す件数の上限（記事生成前の候補数なので多めでよい）
    trend_keywords  : 呼び出し元がすでに持っているトレンドキーワード（任意）
                      指定しなくても内部で Google + X から自動取得する

    失敗時は candidates[:max_select] をそのまま返す。
    """
    if not candidates:
        return []

    if not is_ai_curation_enabled():
        return candidates[:max_select]

    try:
        from app.config import settings
        from openai import OpenAI
        from app.utils.openai_compat import create_with_retry

        if not settings.OPENAI_API_KEY:
            return candidates[:max_select]

        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        use_model = model or getattr(settings, "OPENAI_CURATION_MODEL", None) or "gpt-4o-mini"
    except Exception:
        return candidates[:max_select]

    # ── トレンドキーワードを取得 ──────────────────────────────────
    all_trends = _fetch_all_trends(trend_keywords)
    trend_section = ""
    if all_trends:
        trend_section = (
            "\n\n【現在の急上昇トレンド（Google検索・X(Twitter)）】\n"
            + "、".join(all_trends[:30])
            + "\n↑ これらのキーワードに関連する記事は優先的に選んでください。"
        )
        logger.info("トレンドキーワード %d件をAI選定に使用", len(all_trends))

    # ── 候補を JSON 化 ────────────────────────────────────────────
    items_payload = []
    for i, item in enumerate(candidates):
        items_payload.append({
            "idx": i,
            "title": (getattr(item, "title", "") or "")[:120],
            "summary": (getattr(item, "summary", "") or "")[:250],
            "source": getattr(item, "source", "") or "",
            "category": getattr(item, "category", "") or "",
        })

    system_prompt = (
        "あなたは「知リポAI」というニュースサイトの編集長です。"
        "20〜40代の知的好奇心が高い読者向けに、掲載すべき記事を選びます。\n\n"
        "【選定基準】\n"
        "- 今話題・急上昇中のキーワードに関連する記事を最優先\n"
        "- 社会・経済・テクノロジー・科学・政策など、背景を解説する余地がある\n"
        "- 読者が「これは知りたい」と感じる重要性・話題性がある\n"
        "- 研究・論文は実用的発見や革新的成果を優先\n"
        "- 複数の似た記事があれば最も重要な1件だけを選ぶ\n\n"
        "【除外基準】\n"
        "- スポーツ結果・スコア速報・訃報・芸能ゴシップ\n"
        "- 内容が薄い・情報量が少ない\n"
        "- 同内容の重複"
        + trend_section
    )

    user_prompt = (
        f"以下の記事候補 {len(items_payload)} 件を評価し、掲載すべきものを選んでください。\n\n"
        f"```json\n{json.dumps(items_payload, ensure_ascii=False)}\n```\n\n"
        "返答は以下の JSON 形式のみで（説明不要）:\n"
        '{"selected":[{"idx":0,"score":90},{"idx":3,"score":75},...]}  \n\n'
        f"- idx: 上の候補番号（0〜{len(items_payload)-1}）\n"
        "- score: 掲載優先度 0〜100（トレンドと合致するものほど高く）\n"
        f"- score の高い順に最大 {max_select} 件まで"
    )

    try:
        resp = create_with_retry(
            client,
            512,
            model=use_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        content = (resp.choices[0].message.content or "").strip()
        data = json.loads(content)
        selected_raw = data.get("selected", [])

        if not isinstance(selected_raw, list) or not selected_raw:
            raise ValueError("selected が空またはリストでない")

        # score 降順で並び、有効な idx を持つものだけ採用
        selected_raw.sort(key=lambda x: x.get("score", 0), reverse=True)
        result = []
        seen_idx: set[int] = set()
        for entry in selected_raw:
            idx = entry.get("idx")
            if not isinstance(idx, int) or idx < 0 or idx >= len(candidates):
                continue
            if idx in seen_idx:
                continue
            seen_idx.add(idx)
            result.append(candidates[idx])
            if len(result) >= max_select:
                break

        if not result:
            raise ValueError("有効な選定結果なし")

        logger.info(
            "AI選定完了: 候補 %d件 → 選定 %d件（モデル: %s、トレンド: %d件使用）",
            len(candidates), len(result), use_model, len(all_trends),
        )
        return result

    except Exception as e:
        logger.warning("AI選定に失敗、フォールバック（候補をそのまま使用）: %s", e)
        return candidates[:max_select]
