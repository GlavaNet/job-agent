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
                    provider's base URL — UNLESS LLM_MODEL is left blank,
                    in which case auto-discovery runs instead (see below).
                    Model example: llama-3.1-8b-instant  (Groq)
                                   mistral-small-latest  (Mistral)

Auto-discovery (openai_compatible only)
────────────────────────────────────────
If LLM_BACKEND=openai_compatible and LLM_MODEL is left unset, llm_client
probes a small set of localhost ports at import time to find whichever
local server is actually running (llama-server or Ollama) and asks it
which model is loaded — instead of you hand-editing .env every time you
switch models on the server (e.g. via a script like start-llama.sh that
always launches on the same port but lets you pick a different .gguf
each run).

  LLM_DISCOVER_HOST   Host to probe.  (default: localhost)
  LLM_DISCOVER_PORTS  Comma-separated ports to probe, tried in order —
                      first one that responds wins.  (default: 9001,11434
                      — llama-server's common default, then Ollama's)

Detection per port: tries GET /v1/models first (works for llama-server
and modern Ollama), falls back to GET /api/tags (older Ollama). If no
candidate port responds, startup fails with a clear RuntimeError rather
than silently guessing a model name.

To skip discovery, just set LLM_MODEL explicitly as usual — discovery
only runs when it's blank.

Environment variables (all optional except where noted):
─────────────────────────────────────────────────────────
  LLM_BACKEND       One of the four values above.  (default: ollama)
  LLM_MODEL         Model name sent to the backend. Defaults per backend:
                      ollama            → llama3.1:8b
                      anthropic         → claude-haiku-4-5
                      openai            → gpt-4o-mini
                      openai_compatible → triggers auto-discovery if unset
  LLM_BASE_URL      Base URL for the API endpoint.  Defaults:
                      ollama            → http://localhost:11434
                      anthropic         → https://api.anthropic.com
                      openai            → https://api.openai.com
                      openai_compatible → discovered automatically if
                                          LLM_MODEL is also unset, else
                                          must be set explicitly
  LLM_API_KEY       API key.  Not used for ollama.  For openai_compatible
                    servers with no real auth (e.g. llama-server), set
                    any placeholder value such as "not-needed" — a value
                    is still required so the client doesn't silently send
                    an empty Authorization header, but llama-server itself
                    ignores it.
  LLM_MAX_TOKENS    Default max output tokens for responses. (default: 1024)
                    Can be overridden per-call via invoke_llm(max_tokens=N).
  LLM_DISABLE_THINKING
                    Set to "true" for openai_compatible backends serving a
                    hybrid-thinking model (e.g. Qwen3 via llama.cpp).  Adds
                    chat_template_kwargs: {"enable_thinking": false} to the
                    request body so the model answers directly in `content`
                    instead of spending the whole max_tokens budget on
                    `reasoning_content` and returning empty text.
                    (default: false — no effect on providers that don't
                    recognise the field, but leave unset unless you know
                    your model needs it.)

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

    # With a longer timeout for slow/large generations (e.g. preference engine)
    text = invoke_llm(prompt, timeout=600)
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
# Auto-discovery for openai_compatible backends
#
# For local dev servers (llama-server, Ollama) the running model can change
# between runs — e.g. start-llama.sh lets you interactively pick a different
# .gguf each time it's launched, always on the same port. Rather than
# hand-editing .env every time, LLM_MODEL and/or LLM_BASE_URL can be left
# blank and we'll probe a small set of candidate ports at import time to
# find whichever server is actually up and ask it what's loaded.
#
# Detection order per candidate:
#   1. GET /v1/models   — OpenAI-compatible route. Works for llama-server
#                          (always exactly one model, in `data[0].id`) and
#                          for modern Ollama builds that also expose this.
#   2. GET /api/tags    — Ollama-native fallback for older Ollama builds
#                          that don't serve /v1/models. Returns potentially
#                          multiple pulled models; we take the first, since
#                          there's no cross-version way to ask Ollama which
#                          one is actively loaded in memory without /api/ps,
#                          which itself isn't available on every version.
#
# The first candidate that responds to either probe wins. This is a small,
# fixed list of localhost ports, not a network scan — safe to run on every
# startup.
# ---------------------------------------------------------------------------

_DISCOVER_HOST  = os.getenv("LLM_DISCOVER_HOST", "localhost").strip()
_DISCOVER_PORTS = [
    p.strip() for p in os.getenv("LLM_DISCOVER_PORTS", "9001,11434").split(",") if p.strip()
]
_DISCOVER_TIMEOUT = 2  # seconds — local servers only, fail fast per candidate


def _probe_v1_models(base_url: str) -> str | None:
    """Try the OpenAI-compatible /v1/models route. Returns model id or None."""
    try:
        resp = requests.get(f"{base_url}/v1/models", timeout=_DISCOVER_TIMEOUT)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if data and data[0].get("id"):
            return data[0]["id"]
    except Exception:
        pass
    return None


def _probe_ollama_tags(base_url: str) -> str | None:
    """Try Ollama's native /api/tags route. Returns first model name or None."""
    try:
        resp = requests.get(f"{base_url}/api/tags", timeout=_DISCOVER_TIMEOUT)
        resp.raise_for_status()
        models = resp.json().get("models", [])
        if models and models[0].get("name"):
            return models[0]["name"]
    except Exception:
        pass
    return None


def _discover_server() -> tuple[str, str]:
    """
    Probe LLM_DISCOVER_PORTS on LLM_DISCOVER_HOST for a running llama-server
    or Ollama instance. Returns (base_url, model_name) for the first
    candidate that responds.

    Raises RuntimeError if nothing is found — this is treated as a hard
    startup failure rather than silently falling back to a default model
    name, since a wrong guess here would send every prompt in the pipeline
    to a nonexistent or unintended server.
    """
    for port in _DISCOVER_PORTS:
        base_url = f"http://{_DISCOVER_HOST}:{port}"

        model = _probe_v1_models(base_url)
        if model:
            logger.info(
                "LLM auto-discovery: found model %r via /v1/models at %s",
                model, base_url,
            )
            return base_url, model

        model = _probe_ollama_tags(base_url)
        if model:
            logger.info(
                "LLM auto-discovery: found model %r via /api/tags at %s",
                model, base_url,
            )
            return base_url, model

    raise RuntimeError(
        f"LLM auto-discovery found no running server on {_DISCOVER_HOST}, "
        f"ports {_DISCOVER_PORTS}. Is llama-server or Ollama running? "
        f"Set LLM_BASE_URL and LLM_MODEL explicitly in .env to skip discovery, "
        f"or LLM_DISCOVER_PORTS to include the right port."
    )


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
_env_base_url = os.getenv("LLM_BASE_URL", _defaults["base_url"]).rstrip("/")
_env_model    = os.getenv("LLM_MODEL",    _defaults["model"])
_API_KEY      = os.getenv("LLM_API_KEY",  "")
_MAX_TOKENS   = int(os.getenv("LLM_MAX_TOKENS", "1024"))
_DISABLE_THINKING = os.getenv("LLM_DISABLE_THINKING", "false").strip().lower() in ("true", "1", "yes")

# For openai_compatible, a blank LLM_MODEL (base URL may also be blank)
# triggers auto-discovery instead of the old hard "must be set explicitly"
# requirement. Other backends are unaffected — they have real defaults
# or are validated below as before.
if _BACKEND == "openai_compatible" and not _env_model:
    _BASE_URL, _MODEL = _discover_server()
else:
    _BASE_URL = _env_base_url
    _MODEL    = _env_model

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
    "LLM backend: %s | base_url: %s | model: %s | max_tokens: %d | disable_thinking: %s",
    _BACKEND, _BASE_URL, _MODEL, _MAX_TOKENS, _DISABLE_THINKING,
)


# ---------------------------------------------------------------------------
# Backend implementations
# ---------------------------------------------------------------------------

def _call_ollama(prompt: str, system: str, model: str, max_tokens: int, read_timeout: int) -> str:
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
            timeout=(_CONNECT_TIMEOUT, read_timeout),
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
            f"Ollama timed out after {read_timeout}s (model={model})."
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


def _call_anthropic(prompt: str, system: str, model: str, max_tokens: int, read_timeout: int) -> str:
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
            timeout=(_CONNECT_TIMEOUT, read_timeout),
        )
        resp.raise_for_status()
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(f"Could not connect to Anthropic API at {_BASE_URL}.") from exc
    except requests.exceptions.Timeout as exc:
        raise RuntimeError(f"Anthropic API timed out after {read_timeout}s (model={model}).") from exc
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


def _call_openai_compatible(prompt: str, system: str, model: str, max_tokens: int, read_timeout: int) -> str:
    """
    Call any OpenAI /v1/chat/completions-compatible endpoint.

    Hybrid-thinking models (e.g. Qwen3 served via llama.cpp) emit their
    chain-of-thought into a non-standard `reasoning_content` field and
    can leave `content` empty if max_tokens runs out mid-thought — a
    real risk since these models may "think" for hundreds of tokens
    even on trivial prompts. When LLM_DISABLE_THINKING=true, we send
    llama.cpp's documented chat_template_kwargs override, which makes
    the model answer directly in `content` instead. This field is
    ignored (harmlessly) by providers that don't recognise it.
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload: dict = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if _DISABLE_THINKING:
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    try:
        resp = requests.post(
            f"{_BASE_URL}/v1/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {_API_KEY}", "Content-Type": "application/json"},
            timeout=(_CONNECT_TIMEOUT, read_timeout),
        )
        resp.raise_for_status()
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(f"Could not connect to {_BASE_URL}. Check LLM_BASE_URL in .env.") from exc
    except requests.exceptions.Timeout as exc:
        raise RuntimeError(f"Request timed out after {read_timeout}s (model={model}).") from exc
    except requests.exceptions.HTTPError as exc:
        body = ""
        try:
            body = exc.response.json().get("error", {}).get("message", "")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {exc.response.status_code}: {body or exc}") from exc

    try:
        message = resp.json()["choices"][0]["message"]
        text = message["content"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Unexpected response shape (model={model}): {resp.json()}") from exc

    if not text:
        if message.get("reasoning_content"):
            raise RuntimeError(
                f"Provider returned empty content but non-empty reasoning_content "
                f"(model={model}) — this model spent its whole max_tokens budget "
                f"({max_tokens}) on chain-of-thought. Set LLM_DISABLE_THINKING=true "
                f"in .env if this is a hybrid-thinking model (e.g. Qwen3), or raise "
                f"LLM_MAX_TOKENS substantially."
            )
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
    timeout: int | None = None,
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
    timeout : int | None
        Per-call read timeout in seconds.  Defaults to _READ_TIMEOUT (300).
        Use a higher value for slow or large generations such as the
        preference engine (e.g. timeout=600).

    Returns
    -------
    str
        Response text, stripped of leading/trailing whitespace.

    Raises
    ------
    RuntimeError
        On HTTP failure, empty response, or unparseable response.
    """
    _model        = model      or _MODEL
    _max_tokens   = max_tokens or _MAX_TOKENS
    _system       = system     or ""
    _read_timeout = timeout    or _READ_TIMEOUT

    logger.debug(
        "LLM request → backend=%s model=%s max_tokens=%d prompt_len=%d timeout=%ds",
        _BACKEND, _model, _max_tokens, len(prompt), _read_timeout,
    )

    text = _DISPATCH[_BACKEND](prompt, _system, _model, _max_tokens, _read_timeout)

    logger.debug("LLM response ← %d chars", len(text))
    return text
