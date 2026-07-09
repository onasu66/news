"""OpenAI / Gemini API 互換（max_completion_tokens / temperature）"""
import logging

from app.utils.llm_client import assert_allowed_openai_model, use_gemini

logger = logging.getLogger(__name__)


def _clean_kwargs(kwargs: dict) -> dict:
    """max_tokens を除く（API は max_completion_tokens のみ受け付けるモデルがある）"""
    return {k: v for k, v in kwargs.items() if k != "max_tokens"}


def create_with_retry(client, max_tokens_val: int, *, gemini_task: str | None = None, **create_kwargs):
    """max_completion_tokens のみ使用。AI_PROVIDER=gemini のとき Gemini へ（gemini_task でモデル tier を指定）。"""
    gemini_task = create_kwargs.pop("gemini_task", gemini_task)
    explicit_model = create_kwargs.get("model")
    if use_gemini() and explicit_model and str(explicit_model).startswith("gpt-"):
        from app.config import settings
        from openai import OpenAI

        if not settings.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY が設定されていません")
        oai = OpenAI(api_key=settings.OPENAI_API_KEY)
        kwargs = _clean_kwargs(create_kwargs)
        kwargs["model"] = assert_allowed_openai_model(str(explicit_model))
        logger.info("OpenAI 直接呼び出し: model=%s task=%s", explicit_model, gemini_task or "-")
        return _openai_create_with_retry(oai, max_tokens_val, **kwargs)

    if use_gemini():
        from app.utils.llm_client import GeminiClient, resolve_model

        if not isinstance(client, GeminiClient):
            client = GeminiClient()
        create_kwargs["model"] = resolve_model(create_kwargs.get("model"), task=gemini_task)
        kwargs = _clean_kwargs(create_kwargs)
        return client.chat.completions.create(
            **kwargs,
            max_completion_tokens=max_tokens_val,
            gemini_task=gemini_task,
        )

    kwargs = _clean_kwargs(create_kwargs)
    try:
        return _openai_create_with_retry(client, max_tokens_val, **kwargs)
    except TypeError as e:
        if "max_completion_tokens" in str(e):
            raise RuntimeError(
                "OpenAI API を使うには openai パッケージの更新が必要です。"
                "ターミナルで: pip install -U openai を実行し、サーバーを再起動してください。"
            ) from e
        raise
    except Exception as e:
        err = str(e).lower()
        err_extra = str(getattr(e, "body", "") or getattr(e, "message", "") or "").lower()
        full_err = err + " " + err_extra
        # 400 Bad Request などは原因をログに出す（モデル名・コンテンツポリシー等）
        if "400" in err or "bad request" in err:
            logger.warning("OpenAI API エラー（記事が保存されない原因の可能性）: %s", e)
            body = getattr(e, "body", None) or (getattr(e, "response", None) and getattr(e.response, "text", None))
            if body is not None:
                # 巨大レスポンス本文はログに出さず、サイズのみ記録する
                try:
                    logger.warning("OpenAI response body size: %d chars", len(str(body)))
                except Exception:
                    logger.warning("OpenAI response body: <unavailable>")
        # max_tokens 不可のモデル → 確実に max_tokens を外して再試行
        if "max_tokens" in full_err and "max_completion_tokens" in full_err:
            return _openai_create_with_retry(client, max_tokens_val, **_clean_kwargs(kwargs))
        # temperature 非対応モデル（o1 等）→ temperature=1 または省略で再試行
        if "temperature" in full_err:
            kwargs_no_temp = _clean_kwargs({k: v for k, v in kwargs.items() if k != "temperature"})
            try:
                return _openai_create_with_retry(
                    client, max_tokens_val, temperature=1, **_clean_kwargs(kwargs_no_temp)
                )
            except Exception:
                pass
            return _openai_create_with_retry(client, max_tokens_val, **_clean_kwargs(kwargs_no_temp))
        raise


def _openai_create_with_retry(client, max_tokens_val: int, **kwargs):
    kwargs["model"] = assert_allowed_openai_model(kwargs.get("model"))
    try:
        return client.chat.completions.create(
            **kwargs,
            max_completion_tokens=max_tokens_val,
        )
    except TypeError as e:
        if "max_completion_tokens" in str(e):
            return client.chat.completions.create(
                **_clean_kwargs(kwargs),
                max_tokens=max_tokens_val,
            )
        raise
