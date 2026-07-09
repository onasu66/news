"""OpenAI / Gemini 切り替え用ヘルパー"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

ALLOWED_OPENAI_MODELS = frozenset({
    "gpt-4o-mini",
    "gpt-4o-mini-2024-07-18",
})

GeminiTask = Literal[
    "article",
    "middleman",
    "persona",
    "quality",
    "navigator",
    "lite",
    "translate",
    "title",
    "curation",
    "default",
]

QUALITY_TASKS = frozenset({"article", "middleman", "persona", "quality"})
LITE_TASKS = frozenset({"navigator", "lite", "translate", "title", "curation", "default"})

_pool_lock = threading.Lock()
_pool_index_lite = 0
_pool_index_quality = 0
_model_cooldown_until: dict[str, float] = {}


@dataclass
class _Message:
    content: str | None = None


@dataclass
class _Choice:
    message: _Message


@dataclass
class _ChatCompletionResponse:
    choices: list[_Choice]


class _GeminiChatCompletions:
    def create(self, **kwargs: Any) -> _ChatCompletionResponse:
        gemini_task = kwargs.pop("gemini_task", None)
        model = resolve_model(kwargs.get("model"), task=gemini_task)
        messages = kwargs.get("messages") or []
        temperature = kwargs.get("temperature", 0.7)
        max_tokens = kwargs.get("max_completion_tokens") or kwargs.get("max_tokens") or 1024
        if kwargs.get("response_format"):
            messages = _append_json_hint(messages)
        response_format = kwargs.get("response_format")
        text = _gemini_generate(
            model=model,
            messages=messages,
            temperature=temperature,
            max_output_tokens=int(max_tokens),
            task=gemini_task,
            response_format=response_format,
        )
        return _ChatCompletionResponse(choices=[_Choice(message=_Message(content=text))])


class _GeminiChat:
    completions = _GeminiChatCompletions()


class GeminiClient:
    """OpenAI クライアントと同じ chat.completions.create インターフェース"""

    chat = _GeminiChat()


def ai_provider() -> str:
    from app.config import settings

    return (getattr(settings, "AI_PROVIDER", "openai") or "openai").strip().lower()


def persona_provider() -> str:
    from app.config import settings

    explicit = (getattr(settings, "PERSONA_PROVIDER", "") or "").strip().lower()
    if explicit:
        return explicit
    return ai_provider()


def resolve_persona_model(explicit: str | None = None) -> str:
    """ペルソナコメント用モデル。PERSONA_PROVIDER=gemini かつ gpt 未指定なら Gemini quality プール。"""
    from app.config import settings

    if explicit and str(explicit).strip():
        m = str(explicit).strip()
        return assert_allowed_openai_model(m) if m.startswith("gpt-") else m
    prov = persona_provider()
    openai_named = (getattr(settings, "OPENAI_PERSONA_COMMENT_MODEL", "") or "").strip()
    gemini_named = (getattr(settings, "PERSONA_GEMINI_MODEL", "") or "").strip()
    if use_gemini(prov):
        if openai_named.startswith("gpt-"):
            return assert_allowed_openai_model(openai_named)
        if gemini_named.startswith("gemini-"):
            return gemini_named
        if openai_named.startswith("gemini-"):
            return openai_named
        return resolve_model(None, provider=prov, task="persona")
    return assert_allowed_openai_model(openai_named or settings.OPENAI_MODEL)


def use_gemini(provider: str | None = None) -> bool:
    return (provider or ai_provider()) == "gemini"


def is_ai_configured(provider: str | None = None) -> bool:
    from app.config import settings

    if use_gemini(provider):
        if bool((getattr(settings, "GEMINI_API_KEY", "") or "").strip()):
            return True
        return openai_fallback_enabled() and bool((getattr(settings, "OPENAI_API_KEY", "") or "").strip())
    return bool((getattr(settings, "OPENAI_API_KEY", "") or "").strip())


def openai_fallback_enabled() -> bool:
    """AI_PROVIDER=gemini 時、Gemini 429 後に OpenAI を使うか。"""
    from app.config import settings

    if not use_gemini():
        return False
    if not (getattr(settings, "OPENAI_API_KEY", "") or "").strip():
        return False
    v = (getattr(settings, "OPENAI_FALLBACK_ENABLED", "") or "").strip().lower()
    if v in ("0", "false", "no"):
        return False
    if v in ("1", "true", "yes"):
        return True
    return True


def assert_allowed_openai_model(model: str | None) -> str:
    """Allow only the OpenAI models this app is expected to spend on."""
    m = (model or "").strip()
    if m not in ALLOWED_OPENAI_MODELS:
        raise RuntimeError(f"OpenAI model is not allowed: {m or '(empty)'}")
    return m


def openai_fallback_model(*, task: str | None = None) -> str:
    from app.config import settings

    t = (task or "").strip().lower()
    # ナビは lite だが品質ゲート(500字)があるため quality モデルを使う
    if gemini_task_tier(task) == "quality" or t == "navigator":
        q = (getattr(settings, "OPENAI_FALLBACK_QUALITY_MODEL", "") or "").strip()
        if q:
            return assert_allowed_openai_model(q)
    return assert_allowed_openai_model(
        (getattr(settings, "OPENAI_FALLBACK_MODEL", "") or "gpt-4o-mini").strip()
    )


def _parse_model_list(raw: str, *, fallback: str) -> list[str]:
    seen: set[str] = set()
    pool: list[str] = []
    for part in (raw or fallback).split(","):
        m = part.strip()
        if m and m not in seen:
            seen.add(m)
            pool.append(m)
    return pool or [fallback]


def gemini_lite_pool() -> list[str]:
    from app.config import settings

    raw = (getattr(settings, "GEMINI_MODEL_POOL_LITE", "") or "").strip()
    if not raw:
        raw = (getattr(settings, "GEMINI_MODEL_POOL", "") or "gemini-2.5-flash-lite").strip()
    return _parse_model_list(raw, fallback="gemini-2.5-flash-lite")


def gemini_quality_pool() -> list[str]:
    from app.config import settings

    raw = (getattr(settings, "GEMINI_MODEL_POOL_QUALITY", "") or "").strip()
    if not raw:
        raw = (getattr(settings, "GEMINI_MODEL", "") or "gemini-2.5-flash").strip()
    return _parse_model_list(raw, fallback="gemini-2.5-flash")


def gemini_model_pool() -> list[str]:
    """lite + quality の結合（後方互換）。"""
    seen: set[str] = set()
    merged: list[str] = []
    for m in gemini_lite_pool() + gemini_quality_pool():
        if m not in seen:
            seen.add(m)
            merged.append(m)
    return merged


def gemini_task_tier(task: str | None) -> str:
    t = (task or "lite").strip().lower()
    if t in QUALITY_TASKS:
        return "quality"
    return "lite"


def _pool_for_tier(tier: str) -> list[str]:
    return gemini_quality_pool() if tier == "quality" else gemini_lite_pool()


def _pool_models_available(pool: list[str], now: float | None = None) -> list[str]:
    now = now if now is not None else time.time()
    cooled = [m for m in pool if _model_cooldown_until.get(m, 0) <= now]
    return cooled or pool


def pick_gemini_model(*, tier: str = "lite", preferred: str | None = None) -> str:
    """tier ごとのプールからラウンドロビン。429 クールダウン中はスキップ。"""
    global _pool_index_lite, _pool_index_quality
    now = time.time()
    pool = _pool_for_tier(tier)
    available = _pool_models_available(pool, now)
    if preferred and preferred.startswith("gemini"):
        if preferred in available:
            return preferred
        if preferred in pool and _model_cooldown_until.get(preferred, 0) <= now:
            return preferred
    with _pool_lock:
        if tier == "quality":
            model = available[_pool_index_quality % len(available)]
            _pool_index_quality += 1
        else:
            model = available[_pool_index_lite % len(available)]
            _pool_index_lite += 1
    return model


def mark_gemini_model_cooldown(model: str, seconds: float) -> None:
    until = time.time() + max(1.0, float(seconds))
    with _pool_lock:
        _model_cooldown_until[model] = until
    logger.info("Gemini %s を %.0f秒クールダウン", model, seconds)


def _quality_fallback_enabled() -> bool:
    from app.config import settings

    v = (getattr(settings, "GEMINI_QUALITY_FALLBACK_LITE", "true") or "true").strip().lower()
    return v not in ("0", "false", "no")


def _quota_retry_delay(err: str) -> float:
    m = re.search(r"retry in ([\d.]+)s", err)
    if m:
        return float(m.group(1)) + 1.0
    if "limit: 0" in err:
        return 300.0
    return 55.0


def _is_quota_error(err: str, exc: Exception) -> bool:
    if "429" in err or "quota" in err.lower() or "ResourceExhausted" in type(exc).__name__:
        return True
    return False


def _candidate_models_for_task(*, model: str, task: str | None) -> list[str]:
    tier = gemini_task_tier(task)
    primary_pool = _pool_for_tier(tier)
    candidates: list[str] = []

    def _add(m: str) -> None:
        if m and m not in candidates:
            candidates.append(m)

    if model.startswith("gemini"):
        _add(model)
    for m in primary_pool:
        _add(m)
    if tier == "quality" and _quality_fallback_enabled():
        for m in gemini_lite_pool():
            _add(m)
    return candidates or [model]


def resolve_model(model: str | None = None, *, provider: str | None = None, task: str | None = None) -> str:
    from app.config import settings

    if use_gemini(provider):
        if model and str(model).startswith("gemini"):
            return str(model)
        tier = gemini_task_tier(task)
        return pick_gemini_model(tier=tier)
    if model:
        return assert_allowed_openai_model(str(model))
    return assert_allowed_openai_model(settings.OPENAI_MODEL)


def get_chat_client(*, provider: str | None = None):
    from app.config import settings

    if use_gemini(provider):
        if not settings.GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY が設定されていません")
        return GeminiClient()
    from openai import OpenAI

    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY が設定されていません")
    return OpenAI(api_key=settings.OPENAI_API_KEY)


def _append_json_hint(messages: list[dict]) -> list[dict]:
    if not messages:
        return messages
    out = [dict(m) for m in messages]
    hint = "\n\n出力は有効な JSON のみ。説明文・コードフェンス・前置きは禁止。"
    last = out[-1]
    if last.get("role") == "user":
        last["content"] = str(last.get("content") or "") + hint
    else:
        out.append({"role": "user", "content": hint.strip()})
    return out


def _messages_to_gemini_parts(messages: list[dict]) -> tuple[str, list[dict]]:
    system_parts: list[str] = []
    history: list[dict] = []
    for msg in messages:
        role = (msg.get("role") or "user").strip()
        content = str(msg.get("content") or "").strip()
        if not content:
            continue
        if role == "system":
            system_parts.append(content)
        elif role == "assistant":
            history.append({"role": "model", "parts": [content]})
        else:
            history.append({"role": "user", "parts": [content]})
    system_instruction = "\n\n".join(system_parts).strip()
    return system_instruction, history


def _gemini_generate_once(
    *,
    model: str,
    messages: list[dict],
    temperature: float,
    max_output_tokens: int,
) -> str:
    import google.generativeai as genai
    from app.config import settings

    genai.configure(api_key=settings.GEMINI_API_KEY)
    system_instruction, history = _messages_to_gemini_parts(messages)
    generation_config = genai.types.GenerationConfig(
        temperature=float(temperature),
        max_output_tokens=max(64, int(max_output_tokens)),
    )
    gemini_model = genai.GenerativeModel(
        model_name=model,
        system_instruction=system_instruction or None,
    )
    if not history:
        return ""
    if len(history) == 1 and history[0]["role"] == "user":
        response = gemini_model.generate_content(
            history[0]["parts"][0],
            generation_config=generation_config,
        )
    else:
        last = history[-1]
        prior = history[:-1]
        chat = gemini_model.start_chat(history=prior)
        response = chat.send_message(
            last["parts"][0],
            generation_config=generation_config,
        )
    text = getattr(response, "text", None)
    if text:
        return text.strip()
    try:
        parts = response.candidates[0].content.parts
        return "".join(getattr(p, "text", "") or "" for p in parts).strip()
    except Exception:
        logger.warning("Gemini %s: 空の応答", model)
        return ""


def _openai_fallback_generate(
    *,
    messages: list[dict],
    temperature: float,
    max_output_tokens: int,
    task: str | None = None,
    response_format: dict | None = None,
) -> str:
    from openai import OpenAI
    from app.config import settings

    if not openai_fallback_enabled():
        raise RuntimeError("OpenAI フォールバック無効")
    model = assert_allowed_openai_model(openai_fallback_model(task=task))
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": float(temperature),
        "max_completion_tokens": max(64, int(max_output_tokens)),
    }
    if response_format:
        kwargs["response_format"] = response_format
    logger.info("Gemini 429 → OpenAI %s フォールバック (task=%s)", model, task or "default")
    try:
        response = client.chat.completions.create(**kwargs)
    except TypeError:
        kwargs.pop("response_format", None)
        response = client.chat.completions.create(**kwargs)
    text = (response.choices[0].message.content or "").strip()
    if not text:
        raise RuntimeError("OpenAI フォールバック: 空の応答")
    return text


def _gemini_generate(
    *,
    model: str,
    messages: list[dict],
    temperature: float,
    max_output_tokens: int,
    task: str | None = None,
    response_format: dict | None = None,
) -> str:
    candidates = _candidate_models_for_task(model=model, task=task)
    last_exc: Exception | None = None

    for attempt, current_model in enumerate(candidates):
        try:
            return _gemini_generate_once(
                model=current_model,
                messages=messages,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
            )
        except Exception as e:
            last_exc = e
            err = str(e)
            if not _is_quota_error(err, e):
                raise
            delay = _quota_retry_delay(err)
            mark_gemini_model_cooldown(current_model, delay)
            if attempt >= len(candidates) - 1:
                break
            next_model = candidates[attempt + 1]
            logger.warning(
                "Gemini %s(%s) 429 → %s へ切替 (%d/%d)",
                current_model,
                gemini_task_tier(task),
                next_model,
                attempt + 1,
                len(candidates),
            )
            if delay <= 10 and attempt == 0:
                time.sleep(delay)

    if last_exc and openai_fallback_enabled():
        try:
            return _openai_fallback_generate(
                messages=messages,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                task=task,
                response_format=response_format,
            )
        except Exception as fb_err:
            logger.warning("OpenAI フォールバック失敗: %s", fb_err)

    if last_exc:
        raise last_exc
    return ""
