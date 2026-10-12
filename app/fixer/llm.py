import json
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY")
LLM_MODEL = os.environ.get("LLM_MODEL", "openai/gpt-oss-20b")

LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "60"))

LLM_MAX_ATTEMPTS = 3
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_BACKOFF_S = 10

SUPPORTS_IMAGES = False


class LLMError(Exception):
    pass


class LLMUnavailable(LLMError):
    pass


def _backoff_seconds(response: requests.Response, attempt: int) -> float:
    try:
        wait = float(response.headers.get("Retry-After", ""))
    except ValueError:
        wait = 2.0 ** attempt
    return min(max(wait, 0.0), _MAX_BACKOFF_S)


def llm_chat(
    prompt: str, schema: dict | None = None, images: list[str] | None = None
) -> str:
    if not LLM_API_KEY:
        raise LLMUnavailable(
            "LLM_API_KEY is not set. Get a free key from your provider "
            "(default: https://console.groq.com/keys) and add it to .env"
        )

    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }
    if schema is not None:
        payload["response_format"] = {"type": "json_object"}

    response = None
    for attempt in range(1, LLM_MAX_ATTEMPTS + 1):
        try:
            response = requests.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json=payload,
                timeout=LLM_TIMEOUT,
            )
            if response.status_code in (401, 403):
                raise LLMUnavailable(
                    f"Provider rejected the API key ({response.status_code}). "
                    "Check LLM_API_KEY in .env."
                )
            if response.status_code in _RETRY_STATUSES and attempt < LLM_MAX_ATTEMPTS:
                time.sleep(_backoff_seconds(response, attempt))
                continue
            response.raise_for_status()
            break
        except requests.exceptions.ConnectionError as e:
            raise LLMUnavailable(f"Can't reach {LLM_BASE_URL}: {e}") from e
        except requests.exceptions.RequestException as e:
            raise LLMError(f"LLM request failed: {e}") from e

    try:
        return response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError) as e:
        raise LLMError(f"Unexpected response shape: {e}") from e


def llm_json(prompt: str, schema: dict, images: list[str] | None = None) -> dict:
    raw = llm_chat(prompt, schema, images)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise LLMError(f"Model returned invalid JSON: {e}") from e
