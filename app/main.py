import asyncio
from contextlib import asynccontextmanager, suppress

import asyncpg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.ai.conductor import Conductor
from app.config import get_settings
from app.routes import router
from app.services.game_master import run_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=5)
    app.state.conductor = Conductor()
    game_master = asyncio.create_task(run_loop(app.state.pool, app.state.conductor))
    yield
    game_master.cancel()
    with suppress(asyncio.CancelledError):
        await game_master
    await app.state.pool.close()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Instagram Games API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router, prefix="/v1")
    return app


app = create_app()
