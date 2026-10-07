import json
from dataclasses import dataclass
from typing import Any

from groq import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncGroq,
    RateLimitError,
)

from app.core.config import settings


class GroqProviderError(Exception):
    pass


class GroqNotConfiguredError(GroqProviderError):
    pass


class GroqRateLimitError(GroqProviderError):
    pass


class GroqTimeoutError(GroqProviderError):
    pass


class GroqInvalidAudioError(GroqProviderError):
    pass


class GroqMalformedResponseError(GroqProviderError):
    pass


@dataclass(frozen=True)
class GroqTranscription:
    text: str
    duration_seconds: float | None


class GroqProvider:
    def __init__(
        self,
        *,
        api_key: str | None,
        timeout_seconds: float,
    ) -> None:
        self._api_key = (api_key or "").strip()
        self._timeout_seconds = timeout_seconds

    def _client(self) -> AsyncGroq:
        if not self._api_key:
            raise GroqNotConfiguredError
        return AsyncGroq(
            api_key=self._api_key,
            timeout=self._timeout_seconds,
            max_retries=0,
        )

    async def transcribe_audio(
        self,
        *,
        filename: str,
        content_type: str,
        audio_bytes: bytes,
        model: str,
    ) -> GroqTranscription:
        client = self._client()
        try:
            response = await client.audio.transcriptions.create(
                file=(filename, audio_bytes, content_type),
                model=model,
                response_format="verbose_json",
                temperature=0.0,
            )
        except RateLimitError as exception:
            raise GroqRateLimitError from exception
        except APITimeoutError as exception:
            raise GroqTimeoutError from exception
        except APIConnectionError as exception:
            raise GroqProviderError from exception
        except APIStatusError as exception:
            if exception.status_code in {400, 415, 422}:
                raise GroqInvalidAudioError from exception
            if exception.status_code in {401, 403}:
                raise GroqNotConfiguredError from exception
            raise GroqProviderError from exception
        finally:
            await client.close()

        text = str(getattr(response, "text", "") or "").strip()
        raw_duration = getattr(response, "duration", None)
        try:
            duration = float(raw_duration) if raw_duration is not None else None
        except (TypeError, ValueError):
            duration = None
        return GroqTranscription(text=text, duration_seconds=duration)

    async def extract_structured(
        self,
        *,
        transcript: str,
        model: str,
        system_prompt: str,
        json_schema: dict[str, Any],
    ) -> dict[str, Any]:
        client = self._client()
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"transcript": transcript},
                            ensure_ascii=False,
                        ),
                    },
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "lead_voice_draft",
                        "strict": True,
                        "schema": json_schema,
                    },
                },
                reasoning_effort="low",
                temperature=0.0,
                max_completion_tokens=2500,
            )
        except RateLimitError as exception:
            raise GroqRateLimitError from exception
        except APITimeoutError as exception:
            raise GroqTimeoutError from exception
        except APIConnectionError as exception:
            raise GroqProviderError from exception
        except APIStatusError as exception:
            if exception.status_code in {401, 403}:
                raise GroqNotConfiguredError from exception
            raise GroqProviderError from exception
        finally:
            await client.close()

        try:
            content = response.choices[0].message.content
            if not content:
                raise ValueError
            parsed = json.loads(content)
        except (AttributeError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exception:
            raise GroqMalformedResponseError from exception
        if not isinstance(parsed, dict):
            raise GroqMalformedResponseError
        return parsed


def get_groq_provider() -> GroqProvider:
    secret = settings.groq_api_key
    return GroqProvider(
        api_key=secret.get_secret_value() if secret is not None else None,
        timeout_seconds=settings.groq_request_timeout_seconds,
    )
