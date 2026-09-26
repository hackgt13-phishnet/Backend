"""LLM access behind one small interface: Muse first, any OpenAI-compatible fallback second."""

import json
import os
import re
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class ChatModel:
    base_url: str
    api_key: str
    model: str

    async def complete_json(self, system: str, user: str, timeout: float = 20.0) -> dict:
        """One chat completion that must return a JSON object."""
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            "temperature": 0.4,
        }
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
        ("META_MUSE_BASE_URL", "META_MUSE_API_KEY", "META_MUSE_MODEL", "https://api.meta.ai/v1", "muse-spark-1.3"),
        ("FALLBACK_LLM_BASE_URL", "FALLBACK_LLM_API_KEY", "FALLBACK_LLM_MODEL", "", ""),
    ]
    models = []
    for url_var, key_var, model_var, default_url, default_model in configured:
        key = os.environ.get(key_var, "")
        url = os.environ.get(url_var, default_url)
        model = os.environ.get(model_var, default_model)
        if key and url and model:
            models.append(ChatModel(base_url=url, api_key=key, model=model))
    return models


async def complete_json(system: str, user: str) -> dict | None:
    """Try each configured model in order. None means every model failed or none is configured."""
    for model in models_from_env():
        try:
            return await model.complete_json(system, user)
        except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError):
            continue
    return None
