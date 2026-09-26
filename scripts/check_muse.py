"""One tiny request to confirm the Muse key, model and JSON output work. Costs a fraction of a cent.

uv run --env-file .env python scripts/check_muse.py
"""

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ai.llm import models_from_env


async def main() -> None:
    models = models_from_env()
    if not models:
        sys.exit("no model configured: set META_MUSE_API_KEY (and optionally META_MUSE_MODEL)")
    for model in models:
        start = time.perf_counter()
        try:
            reply = await model.complete_json(
                'Reply with JSON only: {"ok": true, "vibe": "<three lowercase words>"}', "check"
            )
            print(
                f"✓ {model.model} at {model.base_url} · {time.perf_counter() - start:.1f}s · {reply}"
            )
        except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError) as error:
            detail = getattr(getattr(error, "response", None), "text", "")
            print(f"✗ {model.model} at {model.base_url}: {error} {detail[:300]}")


if __name__ == "__main__":
    asyncio.run(main())
