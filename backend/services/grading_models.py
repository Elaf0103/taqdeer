"""
Grading Model Clients
=====================
Thin SDK wrappers that send a rendered prompt to one LLM and return its
raw text response.

SECURITY
--------
This module NEVER reads API keys directly.
All credentials are obtained exclusively from services.llm_clients, which
is the single authorised access point for keys stored in backend/.env.
No key value ever appears in a log line, error message, or return value.

Supported models
----------------
  claude    — Anthropic Claude    (Anthropic SDK)          ← multimodal
  qwen      — Qwen via OpenRouter (OpenAI-compatible SDK)  ← text only
  deepseek  — DeepSeek            (OpenAI-compatible SDK)  ← text only
  openai    — OpenAI GPT-4o       (OpenAI SDK)             ← multimodal

All four clients expose a single method:
    complete(prompt: str, images: list[dict] | None = None) -> str

images, when provided, is a list of dicts:
    {"data": "<base64-encoded bytes>", "media_type": "image/jpeg|image/png|…"}

Claude and OpenAI consume images via their vision APIs.
Qwen and DeepSeek silently ignore the images parameter (text-only path).

The returned string is the raw model output.  JSON parsing is handled
by the grading engine, not here.

Consensus mode
--------------
all_clients() returns ONLY [claude, openai].
Qwen and DeepSeek remain available for individual mode only.

Retry policy
------------
All clients use exponential-backoff retry for transient errors (rate
limits, timeouts, 5xx responses).  Non-retryable errors (4xx auth
failures, validation errors) propagate immediately.
"""

from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod

from services.llm_clients import LLMCredentials, get_credentials


# ── Timeout constants ─────────────────────────────────────────────────────────
# Maximum seconds to wait for a single LLM API call (one HTTP request).
# This is enforced at the SDK level so the TCP connection itself is closed
# when the deadline passes rather than hanging indefinitely.
LLM_CALL_TIMEOUT_SECS: int = 120


# ══════════════════════════════════════════════════════════════════════════════
#  Shared utilities
# ══════════════════════════════════════════════════════════════════════════════

def _with_retry(fn, max_retries: int = 3, base_delay: float = 2.0):
    """
    Exponential-backoff retry wrapper for transient provider errors.

    Retry policy
    ------------
    • RateLimit (429), 5xx server errors → up to max_retries attempts
    • Timeout / Connection errors        → NOT retried (the SDK already
      enforced LLM_CALL_TIMEOUT_SECS; a second attempt would just hang again)
    • All other errors                   → propagate immediately
    """
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as exc:
            cls_name   = type(exc).__name__
            status     = getattr(exc, "status_code", 0) or 0
            err_text   = str(exc).lower()

            # Timeout and connection errors are NOT retried — they fail fast
            # so the consensus-phase ThreadPoolExecutor can apply its own
            # fallback within the _PHASE_TIMEOUT_SECS window.
            if (
                any(kw in cls_name for kw in ("Timeout", "Connection"))
                or "timeout" in err_text
                or "timed out" in err_text
            ):
                raise

            retryable = (
                any(kw in cls_name for kw in (
                    "RateLimit", "ServiceUnavailable",
                    "InternalServer", "Overloaded",
                ))
                or status in (429, 500, 502, 503, 529)
            )
            if not retryable or attempt == max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            print(f"[LLM-RETRY] attempt={attempt + 1}/{max_retries} delay={delay:.0f}s error={cls_name}")
            time.sleep(delay)
    raise RuntimeError("Exhausted retries")  # unreachable — satisfies type checkers


def _clean_output(text: str) -> str:
    """
    Strip thinking-block wrappers and markdown fences from model output.

    DeepSeek-Reasoner and Qwen3 in thinking mode prepend <think>…</think>
    before their actual response.  Some models wrap JSON in ``` fences.
    Both are removed here so the caller always receives bare JSON text.
    """
    # 1. Drop <think>…</think> blocks (DeepSeek-Reasoner / Qwen thinking mode)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    # 2. Drop ```json … ``` or ``` … ``` fences
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$",          "", text.strip())
    return text.strip()


_MAX_TOKENS = 8192


# ══════════════════════════════════════════════════════════════════════════════
#  Health-check error formatter  (shared by all clients)
# ══════════════════════════════════════════════════════════════════════════════

def _format_health_error(
    model_key: str,
    env_var_name: str,
    model_id: str,
    base_url: "str | None",
    exc: Exception,
) -> "tuple[bool, str]":
    """
    Format a human-readable health-check failure message from an exception.

    SECURITY: never includes the raw API key value.
    Shows only the last 4 chars of the key (same masking used in repr).
    """
    import os as _os

    status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)

    # Extract provider error message from structured body (OpenAI/Anthropic SDKs)
    body = getattr(exc, "body", None)
    provider_msg = ""
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict):
            provider_msg = (
                err.get("message", "")
                or err.get("code", "")
                or err.get("type", "")
            )
        elif isinstance(err, str):
            provider_msg = err
    if not provider_msg:
        provider_msg = str(exc)[:400]

    # Masked key suffix — safe to show
    raw_key = _os.environ.get(env_var_name, "")
    key_suffix = f"…{raw_key[-4:]}" if len(raw_key) >= 4 else "(not set)"

    endpoint = base_url or "https://api.openai.com/v1 (default)"

    return False, (
        f"Selected AI model '{model_key}' is not available.\n"
        f"Model      : {model_id}\n"
        f"Status     : {status_code or 'connection error'}\n"
        f"Provider   : {provider_msg}\n"
        f"Endpoint   : {endpoint}\n"
        f"Key source : {env_var_name} (key ending {key_suffix})\n"
        f"\n"
        f"Possible causes:\n"
        f"  • Invalid or expired API key\n"
        f"  • Wrong model slug for this provider (check OPENAI_MODEL / QWEN_MODEL / etc.)\n"
        f"  • Provider account quota / monthly cap reached\n"
        f"  • OpenRouter key requires OPENAI_BASE_URL=https://openrouter.ai/api/v1\n"
        f"  • Backend not restarted after changing .env\n"
        f"  • Model requires image support but provider plan does not include it"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Abstract base
# ══════════════════════════════════════════════════════════════════════════════

class GradingModelClient(ABC):
    """Abstract interface for a grading model client."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier stored in the DB: 'claude' | 'qwen' | 'deepseek' | 'openai'."""

    @abstractmethod
    def complete(self, prompt: str, images: "list[dict] | None" = None) -> str:
        """
        Send the rendered prompt and return the raw text response.

        Parameters
        ----------
        prompt : str
            Fully rendered grading prompt text.
        images : list[dict] | None
            Optional list of base64-encoded images to attach.
            Each dict: {"data": "<base64>", "media_type": "image/jpeg|image/png|…"}
            Clients that do not support vision silently ignore this parameter.
        """

    def health_check(self) -> "tuple[bool, str]":
        """
        Send a minimal test prompt to verify the model is reachable and authenticated.

        Returns (True, "") on success.
        Returns (False, human_readable_error) on any failure — the error string
        includes status_code, provider error message, model name, API endpoint,
        and the env var name (with key masked to last 4 chars).  It NEVER
        contains the raw API key value.
        """
        raise NotImplementedError


# ══════════════════════════════════════════════════════════════════════════════
#  Claude  (Anthropic SDK)
# ══════════════════════════════════════════════════════════════════════════════

class ClaudeGradingClient(GradingModelClient):
    """
    Grading client backed by Anthropic Claude.

    Credentials are read from llm_clients.get_credentials("claude") at
    call time, which reads ANTHROPIC_API_KEY and GRADING_MODEL from the
    environment.  The key is passed directly to the Anthropic() constructor
    and is never stored on this object or logged.
    """

    @property
    def name(self) -> str:
        return "claude"

    def complete(self, prompt: str, images: "list[dict] | None" = None) -> str:
        from anthropic import Anthropic

        creds: LLMCredentials = get_credentials("claude")

        client = Anthropic(api_key=creds.api_key)

        def _call() -> str:
            # Build multimodal content block: images first, then the text prompt.
            # Claude processes images in the order they appear before the text.
            content: list = []
            if images:
                for img in images:
                    content.append({
                        "type": "image",
                        "source": {
                            "type":       "base64",
                            "media_type": img["media_type"],
                            "data":       img["data"],
                        },
                    })
            content.append({"type": "text", "text": prompt})

            response = client.messages.create(
                model       = creds.model_id,
                max_tokens  = _MAX_TOKENS,
                temperature = 0.0,
                messages    = [{"role": "user", "content": content}],
                timeout     = LLM_CALL_TIMEOUT_SECS,
            )
            return response.content[0].text.strip()

        return _clean_output(_with_retry(_call))

    def health_check(self) -> "tuple[bool, str]":
        from anthropic import Anthropic
        creds = get_credentials("claude")
        client = Anthropic(api_key=creds.api_key)
        try:
            resp = client.messages.create(
                model=creds.model_id,
                max_tokens=10,
                messages=[{"role": "user", "content": "Reply with OK"}],
                timeout=30,
            )
            content = (resp.content[0].text if resp.content else "").strip()
            if not content:
                return False, (
                    f"Model 'claude' ({creds.model_id}) returned an empty response.\n"
                    f"Key source: ANTHROPIC_API_KEY"
                )
            return True, ""
        except Exception as exc:
            return _format_health_error("claude", "ANTHROPIC_API_KEY", creds.model_id, None, exc)


# ══════════════════════════════════════════════════════════════════════════════
#  Qwen  (OpenRouter — OpenAI-compatible)
# ══════════════════════════════════════════════════════════════════════════════

class QwenGradingClient(GradingModelClient):
    """
    Grading client backed by Qwen via OpenRouter.

    Credentials are read from llm_clients.get_credentials("qwen") at call
    time, which reads QWEN_API_KEY and QWEN_MODEL from the environment and
    injects the OpenRouter base URL.  The key is passed directly to the
    OpenAI() constructor and is never stored on this object or logged.
    """

    @property
    def name(self) -> str:
        return "qwen"

    def complete(self, prompt: str, images: "list[dict] | None" = None) -> str:
        # images parameter is intentionally ignored — Qwen via OpenRouter
        # is used in individual mode only and receives text-only prompts.
        from openai import OpenAI

        creds: LLMCredentials = get_credentials("qwen")

        client = OpenAI(api_key=creds.api_key, base_url=creds.base_url)

        def _call() -> str:
            response = client.chat.completions.create(
                model       = creds.model_id,
                max_tokens  = _MAX_TOKENS,
                temperature = 0.0,
                messages    = [{"role": "user", "content": prompt}],
                timeout     = LLM_CALL_TIMEOUT_SECS,
            )
            return response.choices[0].message.content or ""

        return _clean_output(_with_retry(_call))

    def health_check(self) -> "tuple[bool, str]":
        from openai import OpenAI
        creds = get_credentials("qwen")
        client = OpenAI(api_key=creds.api_key, base_url=creds.base_url)
        try:
            resp = client.chat.completions.create(
                model=creds.model_id,
                max_tokens=10,
                temperature=0.0,
                messages=[{"role": "user", "content": "Reply with OK"}],
                timeout=30,
            )
            content = (resp.choices[0].message.content or "").strip()
            if not content:
                return False, (
                    f"Model 'qwen' ({creds.model_id}) returned an empty response.\n"
                    f"Endpoint: {creds.base_url}\nKey source: QWEN_API_KEY"
                )
            return True, ""
        except Exception as exc:
            return _format_health_error("qwen", "QWEN_API_KEY", creds.model_id, creds.base_url, exc)


# ══════════════════════════════════════════════════════════════════════════════
#  DeepSeek  (DeepSeek API — OpenAI-compatible)
# ══════════════════════════════════════════════════════════════════════════════

class DeepSeekGradingClient(GradingModelClient):
    """
    Grading client backed by DeepSeek.

    Credentials are read from llm_clients.get_credentials("deepseek") at
    call time, which reads DEEPSEEK_API_KEY and DEEPSEEK_MODEL from the
    environment and injects the DeepSeek base URL.

    For deepseek-reasoner the provider fills a reasoning_content field
    internally; this client reads only the final assistant content so the
    caller always receives the JSON output, not the chain-of-thought.
    """

    @property
    def name(self) -> str:
        return "deepseek"

    def complete(self, prompt: str, images: "list[dict] | None" = None) -> str:
        # images parameter is intentionally ignored — DeepSeek is used in
        # individual mode only and receives text-only prompts.
        import os as _os
        from openai import OpenAI

        creds: LLMCredentials = get_credentials("deepseek")

        client = OpenAI(api_key=creds.api_key, base_url=creds.base_url)

        # deepseek-reasoner (R1) does NOT support temperature; deepseek-chat does.
        # We omit temperature entirely so the same code path works for both models.
        is_reasoner = "reasoner" in creds.model_id.lower()

        print(
            f"[DEEPSEEK-REQUEST] model={creds.model_id} "
            f"endpoint={creds.base_url} "
            f"prompt_chars={len(prompt)} "
            f"is_reasoner={is_reasoner}"
        )

        def _call() -> str:
            kwargs: dict = dict(
                model      = creds.model_id,
                max_tokens = _MAX_TOKENS,
                messages   = [{"role": "user", "content": prompt}],
                timeout    = LLM_CALL_TIMEOUT_SECS,
            )
            # temperature=0 is not supported by deepseek-reasoner; omit for it.
            if not is_reasoner:
                kwargs["temperature"] = 0.0

            response = client.chat.completions.create(**kwargs)
            msg = response.choices[0].message

            # deepseek-reasoner puts the final answer in `content`; the chain-of-
            # thought goes into `reasoning_content`.  If `content` is empty (can
            # happen on some SDK versions), fall back to `reasoning_content` and
            # let _clean_output strip any residual <think> blocks.
            content = msg.content or ""
            if not content:
                reasoning = getattr(msg, "reasoning_content", None) or ""
                if reasoning:
                    print(
                        f"[DEEPSEEK-RESPONSE] model={creds.model_id} "
                        f"content=empty falling_back_to=reasoning_content "
                        f"reasoning_chars={len(reasoning)}"
                    )
                    content = reasoning
                else:
                    print(
                        f"[DEEPSEEK-RESPONSE] model={creds.model_id} "
                        f"content=empty reasoning_content=empty "
                        f"finish_reason={response.choices[0].finish_reason!r}"
                    )
            else:
                print(
                    f"[DEEPSEEK-RESPONSE] model={creds.model_id} "
                    f"content_chars={len(content)} "
                    f"finish_reason={response.choices[0].finish_reason!r}"
                )

            return content

        return _clean_output(_with_retry(_call))

    def health_check(self) -> "tuple[bool, str]":
        from openai import OpenAI
        creds = get_credentials("deepseek")
        client = OpenAI(api_key=creds.api_key, base_url=creds.base_url)
        is_reasoner = "reasoner" in creds.model_id.lower()
        try:
            kwargs: dict = dict(
                model=creds.model_id,
                max_tokens=10,
                messages=[{"role": "user", "content": "Reply with OK"}],
                timeout=30,
            )
            if not is_reasoner:
                kwargs["temperature"] = 0.0
            resp = client.chat.completions.create(**kwargs)
            msg = resp.choices[0].message
            content = (
                msg.content
                or getattr(msg, "reasoning_content", None)
                or ""
            ).strip()
            if not content:
                return False, (
                    f"Model 'deepseek' ({creds.model_id}) returned an empty response.\n"
                    f"Endpoint: {creds.base_url}\nKey source: DEEPSEEK_API_KEY"
                )
            return True, ""
        except Exception as exc:
            return _format_health_error("deepseek", "DEEPSEEK_API_KEY", creds.model_id, creds.base_url, exc)


# ══════════════════════════════════════════════════════════════════════════════
#  OpenAI  (OpenAI SDK — direct API)
# ══════════════════════════════════════════════════════════════════════════════

class OpenAIGradingClient(GradingModelClient):
    """
    Grading client backed by OpenAI GPT-4o.

    Credentials are read from llm_clients.get_credentials("openai") at
    call time, which reads OPENAI_API_KEY and OPENAI_MODEL from the
    environment.  The key is passed directly to the OpenAI() constructor
    and is never stored on this object or logged.
    """

    @property
    def name(self) -> str:
        return "openai"

    def complete(self, prompt: str, images: "list[dict] | None" = None) -> str:
        from openai import OpenAI

        creds: LLMCredentials = get_credentials("openai")

        # base_url is None for a direct OpenAI key (api.openai.com).
        # When OPENAI_BASE_URL is set (e.g. https://openrouter.ai/api/v1),
        # it is forwarded here so OpenRouter keys work transparently.
        client = OpenAI(
            api_key  = creds.api_key,
            base_url = creds.base_url or None,   # None → SDK uses api.openai.com
        )

        def _call() -> str:
            # Build multimodal content block: images first, then the text prompt.
            content: list = []
            if images:
                for img in images:
                    content.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{img['media_type']};base64,{img['data']}",
                        },
                    })
            content.append({"type": "text", "text": prompt})

            response = client.chat.completions.create(
                model       = creds.model_id,
                max_tokens  = _MAX_TOKENS,
                temperature = 0.0,
                messages    = [{"role": "user", "content": content}],
                timeout     = LLM_CALL_TIMEOUT_SECS,
            )
            return response.choices[0].message.content or ""

        return _clean_output(_with_retry(_call))

    def health_check(self) -> "tuple[bool, str]":
        from openai import OpenAI
        creds = get_credentials("openai")
        # Respect OPENAI_BASE_URL override (e.g. for OpenRouter)
        client = OpenAI(api_key=creds.api_key, base_url=creds.base_url or None)
        try:
            resp = client.chat.completions.create(
                model=creds.model_id,
                max_tokens=10,
                temperature=0.0,
                messages=[{"role": "user", "content": "Reply with OK"}],
                timeout=30,
            )
            content = (resp.choices[0].message.content or "").strip()
            if not content:
                return False, (
                    f"Model 'openai' ({creds.model_id}) returned an empty response.\n"
                    f"Endpoint: {creds.base_url or 'https://api.openai.com/v1'}\n"
                    f"Key source: OPENAI_API_KEY"
                )
            return True, ""
        except Exception as exc:
            endpoint = creds.base_url or "https://api.openai.com/v1 (default)"
            return _format_health_error("openai", "OPENAI_API_KEY", creds.model_id, endpoint, exc)


# ══════════════════════════════════════════════════════════════════════════════
#  Factory
# ══════════════════════════════════════════════════════════════════════════════

_CLIENTS: dict[str, GradingModelClient] = {
    "claude":   ClaudeGradingClient(),
    "qwen":     QwenGradingClient(),
    "deepseek": DeepSeekGradingClient(),
    "openai":   OpenAIGradingClient(),
}


def get_model_client(model_name: str) -> GradingModelClient:
    """
    Return the GradingModelClient for the given model identifier.

    Raises ValueError for unknown model names.
    Does NOT pre-validate API key presence — the client validates at
    call time via get_credentials().
    """
    key = model_name.lower().strip()
    client = _CLIENTS.get(key)
    if client is None:
        raise ValueError(
            f"Unknown grading model '{model_name}'. "
            f"Supported: {sorted(_CLIENTS.keys())}"
        )
    return client


def all_clients() -> list[GradingModelClient]:
    """
    Return the two consensus-mode clients in fixed order.

    Consensus assignment:
      Examiner 1 = Claude  (Anthropic)
      Examiner 2 = OpenAI  (GPT-4o)

    Qwen and DeepSeek are excluded from consensus mode.
    They remain accessible via get_model_client() for individual mode.
    """
    return [
        _CLIENTS["claude"],
        _CLIENTS["openai"],
    ]


def run_health_check(model_name: str) -> "tuple[bool, str]":
    """
    Run a live connectivity + auth check for the given model.

    Sends a minimal test prompt ('Reply with OK') and returns:
      (True, "")                — model is reachable and authenticated
      (False, error_message)    — model is unavailable; error_message has
                                  status code, provider error, endpoint,
                                  env var name (key masked). Never contains
                                  the raw API key.

    Raises ValueError for unknown model_name values.
    """
    client = get_model_client(model_name)
    print(f"[HEALTH-CHECK] model={model_name} sending test prompt")
    ok, err = client.health_check()
    if ok:
        print(f"[HEALTH-CHECK] model={model_name} OK")
    else:
        # Log only the first line (no sensitive values in the formatted message,
        # but keep the server log concise)
        first_line = err.split("\n")[0] if err else "unknown error"
        print(f"[HEALTH-CHECK] model={model_name} FAILED: {first_line}")
    return ok, err
