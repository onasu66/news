"""OpenAI API 互換（max_completion_tokens / temperature）"""


def _clean_kwargs(kwargs: dict) -> dict:
    """max_tokens を除く（API は max_completion_tokens のみ受け付けるモデルがある）"""
    return {k: v for k, v in kwargs.items() if k != "max_tokens"}


def create_with_retry(client, max_tokens_val: int, **create_kwargs):
    """max_completion_tokens のみ使用。temperature エラー時は 1 で再試行"""
    kwargs = _clean_kwargs(create_kwargs)
    try:
        return client.chat.completions.create(
            **kwargs,
            max_completion_tokens=max_tokens_val,
        )
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
        # max_tokens 不可のモデル → 確実に max_tokens を外して再試行
        if "max_tokens" in full_err and "max_completion_tokens" in full_err:
            return client.chat.completions.create(
                **_clean_kwargs(kwargs),
                max_completion_tokens=max_tokens_val,
            )
        # temperature 非対応モデル（o1 等）→ temperature=1 または省略で再試行
        if "temperature" in full_err:
            kwargs_no_temp = _clean_kwargs({k: v for k, v in kwargs.items() if k != "temperature"})
            try:
                return client.chat.completions.create(
                    **kwargs_no_temp,
                    temperature=1,
                    max_completion_tokens=max_tokens_val,
                )
            except Exception:
                pass
            return client.chat.completions.create(
                **kwargs_no_temp,
                max_completion_tokens=max_tokens_val,
            )
        raise
