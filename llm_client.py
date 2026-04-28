# llm_client.py
"""
Centralised, provider-agnostic LLM client for the job-agent pipeline.

All LLM-using modules call invoke_llm() and never talk to a backend
directly.  Switching providers requires only changes to .env — no
application code is touched.

Supported backends (set via LLM_BACKEND in .env):
─────────────────────────────────────────────────
  ollama            Local Ollama server (default).
                    Uses /api/generate — no API key required.

  anthropic         Anthropic API (Claude models).
                    Requires LLM_API_KEY.
                    Model example: claude-haiku-4-5

  openai            OpenAI API.
                    Requires LLM_API_KEY.
                    Model example: gpt-4o-mini

  openai_compatible Any provider that speaks the OpenAI /v1/chat/completions
                    API: Groq, Together AI, Mistral, Fireworks, local
                    llama.cpp servers, LM Studio, etc.
                    Requires LLM_API_KEY and LLM_BASE_URL pointed at the
                    provider's base URL.
                    Model example: llama-3.1-8b-instant  (Groq)
                                   mistral-small-latest  (Mistral)

Environment variables (all optional except where noted):
─────────────────────────────────────────────────────────
  LLM_BACKEND       One of the four values above.  (default: ollama)
  LLM_MODEL         Model name sent to the backend. Defaults per backend:
                      ollama            → llama3.1:8b
                      anthropic         → claude-haiku-4-5
                      openai            → gpt-4o-mini
                      openai_compatible → (must be set explicitly)
  LLM_BASE_URL      Base URL for the API endpoint.  Defaults:
                      ollama            → http://localhost:11434
                      anthropic         → https://api.anthropic.com
                      openai            → https://api.openai.com
                      openai_compatible → (must be set explicitly)
  LLM_API_KEY       API key.  Not used for ollama.
  LLM_MAX_TOKENS    Default max output tokens for responses. (default: 1024)
                    Can be overridden per-call via invoke_llm(max_tokens=N).

Legacy Ollama variables are still read as fallbacks so existing .env
files keep working without any changes:
  OLLAMA_BASE_URL   → treated as LLM_BASE_URL when backend is ollama
  OLLAMA_MODEL      → treated as LLM_MODEL when backend is ollama

Usage
─────
    from llm_client import invoke_llm

    # Simple call — all extra params are optional
    text = invoke_llm("Score this job posting...")

    # With a system prompt and tighter output cap
    text = invoke_llm(
        prompt="Extract the job title.",
        system="You are a precise JSON extractor. Return only valid JSON.",
        max_tokens=256,
    )
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------
_CONNECT_TIMEOUT = 10   # seconds — generous for remote API cold-starts
_READ_TIMEOUT    = 300  # seconds — long generations (cover letters, research)


# ---------------------------------------------------------------------------
# Backend configuration — resolved once at import time
# ---------------------------------------------------------------------------

_BACKEND = os.getenv("LLM_BACKEND", "ollama").strip().lower()

_BACKEND_DEFAULTS: dict[str, dict[str, str]] = {
    "ollama": {
        "base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        "model":    os.getenv("OLLAMA_MODEL",    "llama3.1:8b"),
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com",
        "model":    "claude-haiku-4-5",
    },
    "openai": {
        "base_url": "https://api.openai.com",
        "model":    "gpt-4o-mini",
    },
    "openai_compatible": {
        "base_url": "",
        "model":    "",
    },
}

if _BACKEND not in _BACKEND_DEFAULTS:
    raise ValueError(
        f"Unknown LLM_BACKEND={_BACKEND!r}. "
        f"Valid values: {', '.join(_BACKEND_DEFAULTS)}"
    )

_defaults   = _BACKEND_DEFAULTS[_BACKEND]
_BASE_URL   = os.getenv("LLM_BASE_URL", _defaults["base_url"]).rstrip("/")
_MODEL      = os.getenv("LLM_MODEL",    _defaults["model"])
_API_KEY    = os.getenv("LLM_API_KEY",  "")
_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "1024"))

# Validate required vars for API-only backends
if _BACKEND in ("anthropic", "openai") and not _API_KEY:
    raise EnvironmentError(
        f"LLM_API_KEY is required when LLM_BACKEND={_BACKEND!r}. "
        "Add it to your .env file."
    )
if _BACKEND == "openai_compatible":
    missing = [k for k, v in [("LLM_BASE_URL", _BASE_URL), ("LLM_MODEL", _MODEL), ("LLM_API_KEY", _API_KEY)] if not v]
    if missing:
        raise EnvironmentError(
            f"The following are required when LLM_BACKEND=openai_compatible: "
            f"{', '.join(missing)}"
        )

logger.debug(
    "LLM backend: %s | base_url: %s | model: %s | max_tokens: %d",
    _BACKEND, _BASE_URL, _MODEL, _MAX_TOKENS,
)


# ---------------------------------------------------------------------------
# Backend implementations
# ---------------------------------------------------------------------------

def _call_ollama(prompt: str, system: str, model: str, max_tokens: int) -> str:
    """
    Call the Ollama /api/generate endpoint using streaming.

    Streaming is used instead of stream=False so that the requests read
    timeout applies per-chunk rather than to the entire generation.  With
    stream=False, Ollama buffers the full response before sending a single
    byte — on CPU inference this can take several minutes, exhausting the
    read timeout before any data arrives.  Streaming keeps the connection
    alive token-by-token across the whole generation regardless of how
    long it takes.

    Ollama manages its own context window — no num_ctx is sent so the
    model uses whatever it was configured with locally.  The system prompt
    is prepended with a delimiter since /api/generate has no native system
    field.
    """
    import json as _json

    full_prompt = f"{system}\n\n---\n\n{prompt}" if system else prompt

    payload = {
        "model":  model,
        "prompt": full_prompt,
        "stream": True,
    }

    try:
        resp = requests.post(
            f"{_BASE_URL}/api/generate",
            json=payload,
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            stream=True,
        )
        resp.raise_for_status()
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(
            f"Could not connect to Ollama at {_BASE_URL}. "
            "Is the server running?  Check LLM_BASE_URL / OLLAMA_BASE_URL in .env."
        ) from exc
    except requests.exceptions.Timeout as exc:
        raise RuntimeError(
            f"Ollama timed out after {_READ_TIMEOUT}s (model={model})."
        ) from exc
    except requests.exceptions.HTTPError as exc:
        body = ""
        try:
            body = exc.response.json().get("error", "")
        except Exception:
            pass
        raise RuntimeError(
            f"Ollama HTTP {exc.response.status_code}: {body or exc}"
        ) from exc

    tokens: list[str] = []
    for raw_line in resp.iter_lines():
        if not raw_line:
            continue
        try:
            chunk = _json.loads(raw_line)
        except _json.JSONDecodeError:
            continue
        tokens.append(chunk.get("response", ""))
        if chunk.get("done"):
            break

    text = "".join(tokens).strip()
    if not text:
        raise RuntimeError(
            f"Ollama returned an empty response (model={model})."
        )
    return text


def _call_anthropic(prompt: str, system: str, model: str, max_tokens: int) -> str:
    """Call the Anthropic /v1/messages endpoint."""
    headers = {
        "x-api-key":         _API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }
    payload: dict = {
        "model":      model,
        "max_tokens": max_tokens,
        "messages":   [{"role": "user", "content": prompt}],
    }
    if system:
        payload["system"] = system

    try:
        resp = requests.post(
            f"{_BASE_URL}/v1/messages",
            json=payload, headers=headers,
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        resp.raise_for_status()
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(f"Could not connect to Anthropic API at {_BASE_URL}.") from exc
    except requests.exceptions.Timeout as exc:
        raise RuntimeError(f"Anthropic API timed out after {_READ_TIMEOUT}s (model={model}).") from exc
    except requests.exceptions.HTTPError as exc:
        body = ""
        try:
            body = exc.response.json().get("error", {}).get("message", "")
        except Exception:
            pass
        raise RuntimeError(f"Anthropic HTTP {exc.response.status_code}: {body or exc}") from exc

    content_blocks = resp.json().get("content", [])
    text = "".join(b["text"] for b in content_blocks if b.get("type") == "text")
    if not text:
        raise RuntimeError(
            f"Anthropic returned no text content (model={model}). "
            f"Stop reason: {resp.json().get('stop_reason')}"
        )
    return text.strip()


def _call_openai_compatible(prompt: str, system: str, model: str, max_tokens: int) -> str:
    """Call any OpenAI /v1/chat/completions-compatible endpoint."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        resp = requests.post(
            f"{_BASE_URL}/v1/chat/completions",
            json={"model": model, "messages": messages, "max_tokens": max_tokens},
            headers={"Authorization": f"Bearer {_API_KEY}", "Content-Type": "application/json"},
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        resp.raise_for_status()
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(f"Could not connect to {_BASE_URL}. Check LLM_BASE_URL in .env.") from exc
    except requests.exceptions.Timeout as exc:
        raise RuntimeError(f"Request timed out after {_READ_TIMEOUT}s (model={model}).") from exc
    except requests.exceptions.HTTPError as exc:
        body = ""
        try:
            body = exc.response.json().get("error", {}).get("message", "")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {exc.response.status_code}: {body or exc}") from exc

    try:
        text = resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Unexpected response shape (model={model}): {resp.json()}") from exc

    if not text:
        raise RuntimeError(f"Provider returned empty content (model={model}).")
    return text.strip()


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_DISPATCH = {
    "ollama":            _call_ollama,
    "anthropic":         _call_anthropic,
    "openai":            _call_openai_compatible,
    "openai_compatible": _call_openai_compatible,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def invoke_llm(
    prompt: str,
    system: str | None = None,
    model: str | None = None,
    max_tokens: int | None = None,
) -> str:
    """
    Send a prompt to the configured LLM backend and return the response text.

    Parameters
    ----------
    prompt : str
        The user-turn content to send.
    system : str | None
        Optional system prompt.  Handled natively by Anthropic and OpenAI-
        compatible backends.  For Ollama it is prepended to the prompt.
        Defaults to None — all existing call sites work unchanged.
    model : str | None
        Per-call model override.  Defaults to LLM_MODEL / backend default.
    max_tokens : int | None
        Per-call output token limit.  Defaults to LLM_MAX_TOKENS (1024).

    Returns
    -------
    str
        Response text, stripped of leading/trailing whitespace.

    Raises
    ------
    RuntimeError
        On HTTP failure, empty response, or unparseable response.
    """
    _model      = model      or _MODEL
    _max_tokens = max_tokens or _MAX_TOKENS
    _system     = system     or ""

    logger.debug(
        "LLM request → backend=%s model=%s max_tokens=%d prompt_len=%d",
        _BACKEND, _model, _max_tokens, len(prompt),
    )

    text = _DISPATCH[_BACKEND](prompt, _system, _model, _max_tokens)

    logger.debug("LLM response ← %d chars", len(text))
    return text
