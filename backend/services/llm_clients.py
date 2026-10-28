"""
LLM Client Vault  —  backend/services/llm_clients.py
=====================================================
Single, authoritative access point for all LLM API credentials.

SECURITY CONTRACT
-----------------
* API keys MUST be stored only in backend/.env
* API keys MUST be accessed only through this module
* API keys MUST NEVER be:
    - returned in any API response
    - logged to stdout / stderr
    - exposed to frontend JavaScript
    - embedded in prompt strings
    - included in error messages sent over HTTP

This module is the ONLY place in the codebase that calls
os.environ.get("…_API_KEY").  All other modules import from here.

Usage
-----
    from services.llm_clients import get_credentials, assert_keys_configured

    creds = get_credentials("claude")
    client = Anthropic(api_key=creds.api_key)
    model  = creds.model_id

    # Fail fast at endpoint entry if a required key is absent:
    assert_keys_configured(["claude", "qwen"])

Environment variables read by this module
------------------------------------------
    ANTHROPIC_API_KEY   — Claude API key (Anthropic)
    GRADING_MODEL       — Claude model ID   (default: claude-sonnet-4-20250514)

    QWEN_API_KEY        — Qwen API key (OpenRouter)
    QWEN_MODEL          — Qwen model ID    (default: qwen/qwen3-max)

    DEEPSEEK_API_KEY    — DeepSeek API key
    DEEPSEEK_MODEL      — DeepSeek model ID (default: deepseek-chat)

    OPENAI_API_KEY      — OpenAI API key
    OPENAI_MODEL        — OpenAI model ID   (default: gpt-4o-2024-11-20)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


# ── Default model IDs (used when env var is unset) ────────────────────────────

_DEFAULTS: dict[str, tuple[str, Optional[str]]] = {
    # model_key: (default_model_id, base_url or None)
    "claude":   ("claude-sonnet-4-20250514",  None),
    "qwen":     ("qwen/qwen3-max",            "https://openrouter.ai/api/v1"),
    "deepseek": ("deepseek-chat",             "https://api.deepseek.com/v1"),
    "openai":   ("gpt-4o-2024-11-20",         None),   # base_url overridden by OPENAI_BASE_URL
}

_ENV_KEY_MAP: dict[str, str] = {
    "claude":   "ANTHROPIC_API_KEY",
    "qwen":     "QWEN_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openai":   "OPENAI_API_KEY",
}

_ENV_MODEL_MAP: dict[str, str] = {
    "claude":   "GRADING_MODEL",
    "qwen":     "QWEN_MODEL",
    "deepseek": "DEEPSEEK_MODEL",
    "openai":   "OPENAI_MODEL",
}

# Optional base-URL overrides.
# Set OPENAI_BASE_URL=https://openrouter.ai/api/v1 in .env when using an
# OpenRouter key (sk-or-v1-…) instead of a direct OpenAI key (sk-proj-…).
_ENV_BASE_URL_MAP: dict[str, str] = {
    "openai": "OPENAI_BASE_URL",
}


# ══════════════════════════════════════════════════════════════════════════════
#  Credentials dataclass
# ══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class LLMCredentials:
    """
    Immutable bundle of credentials for one LLM provider.

    The api_key field is intentionally private to this dataclass.
    It is accessed only by the model clients that call provider SDKs.
    It MUST NOT be serialised, logged, or returned in HTTP responses.
    """

    model_id: str
    base_url: Optional[str]      # None for Anthropic (SDK handles the URL)

    # Private — stored as a single underscore attribute so dataclass
    # does not include it in __repr__ or __str__ output.
    _api_key: str = ""           # never shown in repr

    def __init__(self, api_key: str, model_id: str, base_url: Optional[str]) -> None:
        # Use object.__setattr__ because frozen=True prevents normal assignment
        object.__setattr__(self, "_api_key", api_key)
        object.__setattr__(self, "model_id",  model_id)
        object.__setattr__(self, "base_url",  base_url)

    @property
    def api_key(self) -> str:
        """
        Return the raw API key string.

        Callers MUST pass this directly to SDK constructors only.
        NEVER include it in log messages, error strings, or HTTP responses.
        """
        return self._api_key  # type: ignore[attr-defined]

    def __repr__(self) -> str:
        # Deliberately omits the key — safe to log this repr
        masked = "***" + (self._api_key[-4:] if len(self._api_key) >= 4 else "***")  # type: ignore[attr-defined]
        return (
            f"LLMCredentials("
            f"model_id={self.model_id!r}, "
            f"base_url={self.base_url!r}, "
            f"api_key={masked!r})"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  Public API
# ══════════════════════════════════════════════════════════════════════════════

def get_credentials(model: str) -> LLMCredentials:
    """
    Return credentials for the requested model, reading from the environment.

    Parameters
    ----------
    model : str
        One of "claude", "qwen", "deepseek".

    Returns
    -------
    LLMCredentials
        Immutable bundle containing api_key, model_id, and base_url.

    Raises
    ------
    ValueError
        If model is not one of the supported identifiers.
    RuntimeError
        If the required API key environment variable is missing or empty.
        The error message names the env var but does NOT include the key value.
    """
    key = model.lower().strip()
    if key not in _DEFAULTS:
        raise ValueError(
            f"Unknown LLM model '{model}'. "
            f"Supported: {sorted(_DEFAULTS.keys())}"
        )

    env_key_name   = _ENV_KEY_MAP[key]
    env_model_name = _ENV_MODEL_MAP[key]
    default_model, default_base = _DEFAULTS[key]

    api_key = os.environ.get(env_key_name, "").strip()
    if not api_key:
        raise RuntimeError(
            f"API key for '{model}' is not configured. "
            f"Set the {env_key_name} environment variable in backend/.env. "
            f"The key must never be hardcoded or sent to the frontend."
        )

    model_id = os.environ.get(env_model_name, "").strip() or default_model

    # Qwen: auto-prefix short model names with "qwen/" for OpenRouter
    if key == "qwen" and "/" not in model_id:
        model_id = f"qwen/{model_id}"

    # Optional base-URL override (e.g. OPENAI_BASE_URL for OpenRouter routing).
    # If the env var is not set, fall back to the compiled default (None for OpenAI).
    base_url_env_var = _ENV_BASE_URL_MAP.get(key)
    if base_url_env_var:
        base_url = os.environ.get(base_url_env_var, "").strip() or default_base
    else:
        base_url = default_base

    return LLMCredentials(
        api_key  = api_key,
        model_id = model_id,
        base_url = base_url,
    )


def assert_keys_configured(models: list[str]) -> None:
    """
    Raise RuntimeError if any required API key is absent.

    Call this at the start of a grading endpoint to fail fast before
    queuing a background job that would immediately error.

    Parameters
    ----------
    models : list[str]
        Model identifiers to check, e.g. ["claude", "qwen", "deepseek"].

    Raises
    ------
    RuntimeError
        With a message naming the missing env var(s) — NOT the key values.
    """
    missing: list[str] = []
    for m in models:
        key = m.lower().strip()
        env_var = _ENV_KEY_MAP.get(key)
        if env_var and not os.environ.get(env_var, "").strip():
            missing.append(f"{m.upper()} ({env_var})")

    if missing:
        raise RuntimeError(
            f"Missing API key(s): {', '.join(missing)}. "
            f"Set the corresponding variables in backend/.env."
        )


def key_is_configured(model: str) -> bool:
    """
    Return True if the API key for model is present and non-empty.

    Safe to call from health-check or status endpoints.
    Does NOT return the key itself.
    """
    key = model.lower().strip()
    env_var = _ENV_KEY_MAP.get(key)
    if not env_var:
        return False
    return bool(os.environ.get(env_var, "").strip())


def configured_models() -> list[str]:
    """
    Return a list of model identifiers whose API keys are present.

    Safe to include in API responses — returns only model names, never keys.
    """
    return [m for m in _DEFAULTS if key_is_configured(m)]
