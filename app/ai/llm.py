"""LLM access behind one small interface: Muse first, any OpenAI-compatible fallback second."""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatModel:
    base_url: str
    api_key: str
    model: str
    reasoning_effort: str | None = None  # Muse Spark reasons by default; "minimal" is ~4x faster

    async def complete_json(self, system: str, user: str, timeout: float = 30.0) -> dict:
        """One chat completion that must return a JSON object."""
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            "temperature": 0.4,
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        async with httpx.AsyncClient(timeout=timeout) as client:
            url = f"{self.base_url.rstrip('/')}/chat/completions"
            headers = {"Authorization": f"Bearer {self.api_key}"}
            response = await client.post(url, headers=headers, json=body)
            if response.status_code in (400, 422):
                # Some providers reject JSON mode; the system prompt already asks for JSON.
                body.pop("response_format")
                response = await client.post(url, headers=headers, json=body)
            response.raise_for_status()
            return parse_json(response.json()["choices"][0]["message"]["content"])


def parse_json(text: str) -> dict:
    """The first JSON object in a reply, tolerating code fences or a sentence around it."""
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise json.JSONDecodeError("not an object", text, 0)
    return value


def models_from_env() -> list[ChatModel]:
    """Muse, then the fallback. Unconfigured providers are skipped."""
    configured = [
        ("META_MUSE", "https://api.meta.ai/v1", "muse-spark-1.3", "minimal"),
        ("FALLBACK_LLM", "", "", None),
    ]
    models = []
    for prefix, default_url, default_model, default_effort in configured:
        key = os.environ.get(f"{prefix}_API_KEY", "")
        url = os.environ.get(f"{prefix}_BASE_URL", default_url)
        model = os.environ.get(f"{prefix}_MODEL", default_model)
        effort = os.environ.get(f"{prefix}_REASONING_EFFORT", default_effort) or None
        if key and url and model:
            models.append(ChatModel(base_url=url, api_key=key, model=model, reasoning_effort=effort))
    return models


async def complete_json(system: str, user: str, attempts: int = 2) -> dict | None:
    """Try each configured model in order, and the whole chain twice. None means every call failed
    or no model is configured; callers fall back to templates, so failures are logged, not raised."""
    models = models_from_env()
    for attempt in range(attempts if models else 0):
        for model in models:
            try:
                return await model.complete_json(system, user)
            except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError) as error:
                log.warning("LLM call failed (%s, attempt %d): %s", model.model, attempt + 1, error)
        await asyncio.sleep(1.0)
    return None
