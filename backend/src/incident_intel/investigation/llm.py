"""The LLM provider interface, and the Gemini implementation.

A provider does one thing: turn a system instruction and a user message into JSON text.
It has no tools and no access to anything else.
"""

import json
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import structlog
from pydantic import Field, SecretStr, field_validator

from incident_intel.core.config import EncryptionSettings, RuntimeSettings

logger = structlog.get_logger(__name__)

_MAX_ERROR_DETAIL = 200


class WorkerSettings(RuntimeSettings, EncryptionSettings):
    """Settings for the investigation worker: the only process that holds the LLM key. It
    also reads stored integration secrets (a project's GitHub token) to collect evidence."""

    gemini_api_key: SecretStr | None = None
    gemini_model: str = Field(default="gemini-3.5-flash", pattern=r"^[A-Za-z0-9._-]{1,80}$")
    gemini_base_url: str = "https://generativelanguage.googleapis.com"
    llm_timeout_seconds: float = Field(default=90.0, gt=0)
    llm_max_output_tokens: int = Field(default=8192, ge=256)
    # Tries per model call when the provider is rate limited or temporarily failing.
    llm_max_attempts: int = Field(default=4, ge=1)

    worker_poll_seconds: float = Field(default=2.0, gt=0)
    # A running investigation not finished within this time is assumed to belong to a
    # worker that died, and is picked up again.
    investigation_lease_seconds: int = Field(default=600, ge=30)
    investigation_max_attempts: int = Field(default=2, ge=1)

    @field_validator("gemini_api_key", mode="before")
    @classmethod
    def _empty_means_unset(cls, value: object) -> object:
        return None if value == "" else value


@dataclass(frozen=True)
class LLMResponse:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class LLMError(Exception):
    """The provider could not produce a response. ``transient`` means a retry may work."""

    def __init__(self, message: str, *, transient: bool, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.transient = transient
        self.retry_after_seconds = retry_after_seconds


class LLMProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    async def generate_json(self, *, system: str, user: str) -> LLMResponse:
        """Return the model's reply, which is asked to be a single JSON document."""
        ...

    async def close(self) -> None: ...


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        timeout_seconds: float,
        max_output_tokens: int,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self._max_output_tokens = max_output_tokens
        # The key travels in a header, never in the URL, so it cannot end up in URL logs.
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds))
        self._headers = {"x-goog-api-key": api_key}
        self._url = f"{base_url.rstrip('/')}/v1beta/models/{model}:generateContent"

    async def generate_json(self, *, system: str, user: str) -> LLMResponse:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.2,
                "maxOutputTokens": self._max_output_tokens,
            },
        }
        try:
            response = await self._http.post(self._url, json=body, headers=self._headers)
        except httpx.TransportError as exc:
            raise LLMError(
                f"could not reach Gemini ({type(exc).__name__})", transient=True
            ) from exc

        if response.status_code != 200:
            raise _http_error(response)
        try:
            payload: dict[str, Any] = response.json()
        except json.JSONDecodeError as exc:
            raise LLMError("Gemini returned a response that is not JSON", transient=True) from exc

        candidates = payload.get("candidates") or []
        if not candidates:
            reason = (payload.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise LLMError(f"Gemini returned no answer ({reason})", transient=False)
        candidate = candidates[0]
        finish = candidate.get("finishReason")
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
        if finish == "MAX_TOKENS":
            raise LLMError("Gemini's answer was cut off (output token limit)", transient=False)
        if not text:
            raise LLMError(f"Gemini returned an empty answer ({finish})", transient=False)
        usage = payload.get("usageMetadata") or {}
        return LLMResponse(
            text=text,
            input_tokens=usage.get("promptTokenCount"),
            output_tokens=usage.get("candidatesTokenCount"),
        )

    async def close(self) -> None:
        await self._http.aclose()


def _http_error(response: httpx.Response) -> LLMError:
    status = response.status_code
    detail = ""
    try:
        message = response.json().get("error", {}).get("message", "")
        detail = str(message)[:_MAX_ERROR_DETAIL]
    except (json.JSONDecodeError, AttributeError):
        detail = ""
    transient = status in {408, 429} or status >= 500
    retry_after: float | None = None
    try:
        retry_after = float(response.headers.get("Retry-After", ""))
    except ValueError:
        retry_after = None
    return LLMError(
        f"Gemini answered {status}{': ' + detail if detail else ''}",
        transient=transient,
        retry_after_seconds=retry_after,
    )


class DisabledProvider:
    """Used when no API key is configured: investigations fail with a clear message."""

    name = "none"
    model = "none"

    async def generate_json(self, *, system: str, user: str) -> LLMResponse:
        raise LLMError(
            "No LLM is configured. Set GEMINI_API_KEY for the investigation worker.",
            transient=False,
        )

    async def close(self) -> None:
        return None


def build_provider(settings: WorkerSettings) -> LLMProvider:
    if settings.gemini_api_key is None:
        logger.warning("llm_disabled", reason="GEMINI_API_KEY is not set")
        return DisabledProvider()
    return GeminiProvider(
        api_key=settings.gemini_api_key.get_secret_value(),
        model=settings.gemini_model,
        base_url=settings.gemini_base_url,
        timeout_seconds=settings.llm_timeout_seconds,
        max_output_tokens=settings.llm_max_output_tokens,
    )
