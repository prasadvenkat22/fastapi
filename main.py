import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import routes.auth_router as auth_router
import routes.users_router as users_router
import routes.db_pgrs_router as db_pgrs_router
import routes.dbfile_pgrs_router as dbfile_pgrs_router
import routes.image_router as image_router
import routes.trading_router as trading_router
import GENAI.router as genai_router
from helpers.auth_deps import require_admin, require_trading

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

# Authentication is enforced HERE rather than route by route, so that what
# each router requires is readable in one place and a newly added endpoint
# inherits its router's protection instead of defaulting to public.
#
# Until 2026-08-23 every line below was bare: /auth/login issued real tokens
# and nothing consumed them, so DELETE /CRUD/users/{id} would delete any
# admin for anyone who could reach the host.
#
# admin-only rather than a read/write split for now, because all four
# accounts that exist ARE admins -- the `user` role's documented read access
# needs per-endpoint dependencies across ~40 CRUD routes, and claiming it
# here without them would be a lie in the one file people check.

# Open, and must stay open: /auth/login is how you get a token. /auth/me and
# /auth/change-password carry their own CurrentUser dependency.
app.include_router(auth_router.router)

# Its own dependency is declared on the router, not repeated here.
app.include_router(users_router.router)

app.include_router(db_pgrs_router.router, dependencies=[Depends(require_admin())])
app.include_router(dbfile_pgrs_router.router, dependencies=[Depends(require_admin())])
app.include_router(image_router.router, dependencies=[Depends(require_admin())])

# admin OR trader: the one router a trader account exists to reach.
app.include_router(trading_router.router, dependencies=[Depends(require_trading())])

# Protected as much for the bill as for the data -- every call spends
# Anthropic and Voyage credit.
app.include_router(genai_router.router, dependencies=[Depends(require_admin())])


@app.get("/")
async def home():
    return {"status": "ok", "app": "FastAPI E-Commerce Backend (Postgres)"}
