import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import routes.auth_router as auth_router
import routes.db_pgrs_router as db_pgrs_router
import routes.dbfile_pgrs_router as dbfile_pgrs_router
import routes.image_router as image_router
import routes.trading_router as trading_router
import GENAI.router as genai_router

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Create any missing Postgres tables on startup.

    Best-effort on purpose: a database that is briefly unreachable should not
    stop the app from booting, and alembic owns the real schema anyway --
    this only covers a fresh database that has never been migrated.
    """
    try:
        from config.db_pgrs import engine
        import models_pgdb.models as models
        models.Base.metadata.create_all(bind=engine)
    except Exception as exc:
        logging.getLogger(__name__).warning("Postgres create_all failed: %s", exc)
    yield


app = FastAPI(
    title="FastAPI E-Commerce Backend",
    description="Postgres-powered e-commerce API",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(auth_router.router)
app.include_router(db_pgrs_router.router)
app.include_router(dbfile_pgrs_router.router)
app.include_router(image_router.router)
app.include_router(trading_router.router)
app.include_router(genai_router.router)


@app.get("/")
async def home():
    return {"status": "ok", "app": "FastAPI E-Commerce Backend (Postgres)"}
