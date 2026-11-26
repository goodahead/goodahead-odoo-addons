# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
import logging

import requests

_logger = logging.getLogger(__name__)


API_URL = "https://api.anthropic.com/v1/messages"
MODELS_URL = "https://api.anthropic.com/v1/models"
API_VERSION = "2023-06-01"
# 🔴 Q3, 2026-08-28 — LAST RESORT ONLY. The ORM layer should never reach this.
#
# This was `claude-sonnet-4-20250514`, which is retired: every call that did not name a model
# was aimed at an id the API no longer serves, and the failure is a 404 at call time that no
# gate can see. `goodahead.claude.client._resolve_model()` now resolves the default from the
# models FETCHED into `goodahead.claude.model` and raises a useful error if it cannot, so this
# constant only applies to direct `lib.claude_client` callers that bypass the ORM entirely.
#
# ⚠️ It is a literal, so it will go stale again — that is the nature of the thing, and the
# reason the ORM path no longer depends on it. Current model ids carry NO date suffix; do not
# "restore" one.
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_OUTPUT_TOKENS = 4096
DEFAULT_TIMEOUT = 120


class ClaudeAPIError(Exception):
    """Raised when the Claude API call fails or returns an unexpected response."""


def _api_headers(api_key):
    return {
        "x-api-key": api_key,
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }


def _wrap_static_system(system):
    """Wrap a plain string `system` prompt as a single cacheable text block.

    Accepts:
    * None -> returned unchanged
    * str -> wrapped as [{"type": "text", "text": str, "cache_control": {...}}]
    * list -> returned unchanged (caller already in structured form;
      cache_control should be added on the relevant block)
    """
    if system is None or isinstance(system, list):
        return system
    if isinstance(system, str):
        return [{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }]
    raise ClaudeAPIError(
        f"Unsupported `system` type: {type(system).__name__}; expected str, list, or None"
    )


def messages_create(
    api_key,
    model,
    messages,
    system=None,
    max_tokens=None,
    cache_static_system=True,
    timeout=DEFAULT_TIMEOUT,
):
    """Call the Claude messages API.

    Args:
        api_key: Anthropic API key.
        model: model id (e.g. "claude-opus-5"). Current ids carry no date suffix.
        messages: list of {"role": "user"|"assistant", "content": ...} dicts.
        system: optional system prompt. May be:
            * str: a plain system prompt — automatically wrapped as a single
              cacheable text block when `cache_static_system=True`.
            * list: pre-structured list of content blocks; the caller controls
              `cache_control` placement.
            * None: omitted.
        max_tokens: model output limit (defaults to DEFAULT_MAX_OUTPUT_TOKENS).
        cache_static_system: if True (default), a plain-string `system` prompt
            is wrapped with ephemeral cache_control. Set to False to disable.
        timeout: request timeout in seconds.

    Returns:
        Parsed JSON response dict from the API. The caller is responsible for
        extracting text content / usage / stop_reason.

    Raises:
        ClaudeAPIError on transport error or non-200 response.
    """
    if not api_key:
        raise ClaudeAPIError("Claude API key is not configured")
    if not model:
        raise ClaudeAPIError("Claude model is not specified")

    if cache_static_system:
        system_payload = _wrap_static_system(system)
    else:
        system_payload = system

    payload = {
        "model": model,
        "max_tokens": max_tokens or DEFAULT_MAX_OUTPUT_TOKENS,
        "messages": messages,
    }
    if system_payload is not None:
        payload["system"] = system_payload

    try:
        resp = requests.post(
            API_URL,
            headers=_api_headers(api_key),
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise ClaudeAPIError(f"Claude API request failed: {e}") from e

    if resp.status_code != 200:
        raise ClaudeAPIError(
            f"Claude API returned {resp.status_code}: {resp.text[:500]}"
        )

    try:
        return resp.json()
    except ValueError as e:
        raise ClaudeAPIError(
            f"Claude API returned non-JSON response: {resp.text[:500]}"
        ) from e


def test_connection(api_key, model=None):
    """Send a minimal call to verify the API key works. Raises on failure."""
    return messages_create(
        api_key=api_key,
        model=model or DEFAULT_MODEL,
        messages=[{"role": "user", "content": "Reply OK"}],
        max_tokens=10,
        cache_static_system=False,
        timeout=30,
    )


def list_models(api_key):
    """Fetch available models from the API. Returns a list of model dicts."""
    all_models = []
    params = {"limit": 100}
    while True:
        try:
            resp = requests.get(
                MODELS_URL,
                headers=_api_headers(api_key),
                params=params,
                timeout=30,
            )
        except requests.RequestException as e:
            raise ClaudeAPIError(f"Models API request failed: {e}") from e

        if resp.status_code != 200:
            raise ClaudeAPIError(
                f"Models API returned {resp.status_code}: {resp.text[:500]}"
            )
        try:
            data = resp.json()
        except ValueError as e:
            raise ClaudeAPIError(
                f"Models API returned non-JSON response: {resp.text[:500]}"
            ) from e
        all_models.extend(data.get("data", []))
        if not data.get("has_more") or not all_models:
            break
        params["after_id"] = all_models[-1]["id"]
    return all_models


def extract_text(response):
    """Concatenate all text blocks in a Claude `messages` response."""
    return "".join(
        block.get("text", "")
        for block in response.get("content", [])
        if block.get("type") == "text"
    )


def extract_usage(response):
    """Return token usage from a Claude `messages` response.

    Includes prompt-cache fields if present.
    """
    usage = response.get("usage", {}) or {}
    return {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
    }
