"""Live Crusoe inference adapter implementing the ModelClient protocol (OpenAI-compatible API).

Exactly one HTTP request per complete() call: retry and fallback belong to calls.py, so there
is no retry, sleep or backoff here. Model ids are case-sensitive and spelled differently across
doc sources, so they must be resolved against the live /models listing (resolve_model), never
hard-coded. Prices are conservative: total tokens at the output rate. The API key is kept out
of repr and is redacted from every error message.
"""

import http.client
import json
import math
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from optimizer_core.config import read_environment

from .budget import estimate_prompt_tokens
from .models import ModelError, ModelReply

DEFAULT_BASE_URL = "https://api.inference.crusoecloud.com/v1"

# (lowercase substring, output USD per 1M tokens). First match wins, so specific patterns go first.
OUTPUT_USD_PER_1M = (
    ("nemotron-3-super", 2.40),
    ("nemotron-3.5-super-vl", 0.40),
    ("nemotron-3-nano-omni", 1.83),
    ("nemotron-3-nano", 0.20),
    ("nemotron-3.5-lightning", 0.20),
    ("nemotron-3-5-lightning", 0.20),
    ("deepseek-v4.1-flash", 1.20),
    ("deepseek-v4-1-flash", 1.20),
    ("deepseek-v4-flash", 0.28),
    ("deepseek-v4-pro", 3.48),
    ("glm-5.3-flash", 0.50),
    ("glm-5-3-flash", 0.50),
    ("glm-5.3", 4.40),
    ("glm-5-3", 4.40),
    ("gemma-4-31b", 0.40),
    ("gpt-oss-120b", 0.25),
)
_MAX_ERROR_CHARS = 300
_CHUNK_BYTES, _MAX_BODY_BYTES = 64 * 1024, 8 * 1024 * 1024
# Everything urllib/http.client may raise on the wire (HTTPException and ValueError are not OSErrors).
_TRANSPORT_ERRORS = (urllib.error.URLError, OSError, http.client.HTTPException, ValueError)


def price_per_1k(model_id):
    """Conservative USD per 1k total tokens (the output rate), or None if the model is unknown."""
    lowered = str(model_id).lower()
    for pattern, per_million in OUTPUT_USD_PER_1M:
        if pattern in lowered:
            return per_million / 1000
    return None


def prices_for(model_ids):
    """model_id -> USD per 1k tokens for Budget(prices=...). Live runs must be priced."""
    prices, missing = {}, []
    for model_id in model_ids:
        price = price_per_1k(model_id)
        if price is None:
            missing.append(model_id)
        else:
            prices[model_id] = price
    if missing:
        raise ValueError(f"no price known for model(s): {', '.join(sorted(set(missing)))}")
    return prices


def resolve_model(wanted, available):
    """Map a wanted name onto a live model id: exact id or name after the org prefix (any case),
    else a unique substring."""
    needle = wanted.lower()
    exact = [m for m in available if needle in (m.lower(), m.lower().rsplit("/", 1)[-1])]
    if len(exact) == 1:
        return exact[0]
    matches = exact or [m for m in available if needle in m.lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"no model matches {wanted!r}; available: {', '.join(sorted(available))}")
    raise ValueError(f"{wanted!r} is ambiguous: {', '.join(sorted(matches))}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect so the Authorization header can never follow a 3xx elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _build_opener():
    return urllib.request.build_opener(_NoRedirect)


def _read_bounded(response, deadline):
    """Read a body in chunks under one wall-clock deadline and a size cap (timeout is per socket op)."""
    read = getattr(response, "read1", response.read)
    chunks, size = [], 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ModelError("crusoe response exceeded time/size limit")
        # Bound the next socket read by the TOTAL remaining time, not the original timeout.
        socket = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if socket is not None:
            socket.settimeout(remaining)
        try:
            chunk = read(_CHUNK_BYTES)
        except TimeoutError:
            raise ModelError("crusoe response exceeded time/size limit") from None
        if time.monotonic() > deadline:
            raise ModelError("crusoe response exceeded time/size limit")
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > _MAX_BODY_BYTES or time.monotonic() > deadline:
            raise ModelError("crusoe response exceeded time/size limit")
        chunks.append(chunk)


def _urllib_opener(method, url, headers, body, timeout):
    deadline = time.monotonic() + timeout
    try:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with _build_opener().open(request, timeout=timeout) as response:
                return response.status, _read_bounded(response, deadline)
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, _read_bounded(exc, deadline)
            finally:
                exc.close()
    except _TRANSPORT_ERRORS as exc:
        raise ModelError(f"crusoe transport error: {type(exc).__name__}") from None


class CrusoeClient:
    def __init__(self, *, api_key, base_url=DEFAULT_BASE_URL, timeout=120.0, opener=None,
                 model_params=None):
        try:
            url = urlparse(base_url)
            url.port
        except ValueError:
            raise ValueError("Crusoe API URL is malformed or has an invalid port") from None
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query:
            raise ValueError("Crusoe API URL must be HTTPS without credentials/query")
        if not api_key:
            raise ValueError("Add CRUSOE_API_KEY to the local .env file first")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not timeout > 0:
            raise ValueError("invalid Crusoe timeout")
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = opener or _urllib_opener
        self._model_params = {str(k).lower(): dict(v) for k, v in (model_params or {}).items()}

    def __repr__(self):
        return f"CrusoeClient(base_url={self.base_url!r})"

    @classmethod
    def from_env(cls, path=".env", **kw):
        env = read_environment(path)
        key = env.get("CRUSOE_API_KEY", "")
        if not key:
            raise ValueError("Add CRUSOE_API_KEY to the local .env file first")
        if "model_params" not in kw:
            from .config import load_config  # per-model request options, e.g. reasoning off
            kw["model_params"] = load_config().get("model_params", {})
        return cls(api_key=key, base_url=env.get("CRUSOE_API_URL") or DEFAULT_BASE_URL, **kw)

    def _clean(self, text):
        return str(text).replace(self._api_key, "[redacted]")[:_MAX_ERROR_CHARS]

    def _request(self, method, path, payload=None, *, timeout_seconds=None):
        """One HTTP request; returns parsed JSON or raises ModelError (never leaks the key)."""
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        timeout = self.timeout
        if timeout_seconds is not None:
            if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                    or timeout_seconds <= 0):
                raise ModelError("model call has no valid remaining deadline")
            timeout = min(timeout, timeout_seconds)
        try:
            status, raw = self._opener(method, f"{self.base_url}{path}", headers, body, timeout)
        except ModelError as exc:
            raise ModelError(self._clean(exc)) from None
        except _TRANSPORT_ERRORS as exc:
            raise ModelError(self._clean(f"crusoe transport error: {type(exc).__name__}")) from None
        text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        if type(status) is int and 300 <= status < 400:
            raise ModelError(f"crusoe HTTP {status}: redirect refused")
        if type(status) is not int or not 200 <= status < 300:
            raise ModelError(self._clean(f"crusoe HTTP {status}: {_error_message(text)}"))
        try:
            return json.loads(text)
        except (ValueError, RecursionError):
            raise ModelError("crusoe returned malformed JSON") from None

    def complete_with_timeout(self, model_id, role, messages, max_tokens, timeout_seconds):
        return self.complete(model_id, role, messages, max_tokens, timeout_seconds=timeout_seconds)

    def complete(self, model_id, role, messages, max_tokens, *, timeout_seconds=None):
        payload = {**self._model_params.get(model_id.lower(), {}),
                   "model": model_id, "messages": messages, "max_tokens": max_tokens}
        data = self._request("POST", "/chat/completions", payload, timeout_seconds=timeout_seconds)
        usage = self._reported_tokens(data)
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ModelError("crusoe response missing choices[0].message.content", tokens=usage) from None
        finish = choice.get("finish_reason") if isinstance(choice, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ModelError(self._clean(f"crusoe empty content (finish_reason={finish})"), tokens=usage)
        return ModelReply(text=content, tokens=self._tokens(data, messages, max_tokens))

    @staticmethod
    def _reported_tokens(data):
        usage = data.get("usage") if isinstance(data, dict) else None
        usage = usage if isinstance(usage, dict) else {}

        def valid(value):
            return type(value) is int and value >= 0
        counts = []
        if valid(usage.get("total_tokens")):
            counts.append(usage["total_tokens"])
        if valid(usage.get("prompt_tokens")) and valid(usage.get("completion_tokens")):
            counts.append(usage["prompt_tokens"] + usage["completion_tokens"])
        best = max(counts, default=0)
        return best if best > 0 else None

    @staticmethod
    def _tokens(data, messages, max_tokens):
        reported = CrusoeClient._reported_tokens(data)
        return reported if reported is not None else estimate_prompt_tokens(messages) + max_tokens

    def list_models(self):
        data = self._request("GET", "/models")
        try:
            ids = [item["id"] for item in data["data"]]
        except (KeyError, TypeError):
            raise ModelError("crusoe /models response has an unexpected shape") from None
        if not all(isinstance(i, str) and i for i in ids):
            raise ModelError("crusoe /models response has an invalid model id")
        return sorted(set(ids))


def _error_message(text):
    """Best-effort short message from an error body (JSON {"error": {...}} or raw text)."""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return text.strip()
    error = data.get("error", data) if isinstance(data, dict) else data
    if isinstance(error, dict):
        error = error.get("message") or error.get("detail") or error.get("code") or error
    return str(error).strip()
