# Fastapi

Postgres-backed FastAPI service. Set the env file before calling the API endpoints.
See ARCHITECTURE.md for what actually runs, deployment_notes.txt for the stack
and deploy steps, and strategy_notes.txt for the decision record behind every
number.

Three books share the engine: an automated 0DTE QQQ strategy on a one-minute
cron, an observational weekly single-name book with one live slice, and manual
positions the engine manages once opened. A news pipeline tags headlines to
tickers, grades same-day sentiment, and labels each one with the price move
that followed -- observationally; nothing in it gates a trade. Analysis scripts
(`iv_rv_screen`, `weekly_pick`, `delta_calibration`, `sweep`) are run by hand
and place no orders.

READ THE BANNER, NOT THE HEADING. Any sweep or screen prints the configuration
it ran at. The repo `.env` mirrors production with two deliberate inversions
(`TRADING_LIVE_ORDERS=false`, `TRADING_ORDER_PREVIEW_ONLY=true`) so a local run
cannot route an order; every figure is only as good as the banner above it.

Accounts: admins manage users at `/users` (create with a role, change role, disable,
delete, reset a forgotten password) using a token from `POST /auth/login`. Users
change their own password at `POST /auth/change-password`, or recover a
forgotten one at `POST /auth/forgot-password` (needs SMTP configured). `scripts/add_user.py` and
`scripts/set_password.py` do the same against the database directly, and are the way
back when nobody can log in. See auth_notes.txt.

Public sign-up (2026-09-23): `POST /auth/signup` creates a role `user` account that
cannot log in until the mailed link is spent at `POST /auth/verify-email` (needs
`APP_BASE_URL` and mail configured). The reply is the same whether or not the
address exists. A `user` gets 403 from every `/trading` route.

Trading settings (2026-09-23): stop loss, stall, give-back, budgets and the other
whitelisted knobs are tuned without a restart through `trading_overrides.env`,
from `/desk/settings` (`GET`/`PUT /trading/settings`, admin to write) or
`scripts/settings.py` on the droplet. `commands.txt` has the copy-paste commands.

Every router except `/auth` requires a token, applied in `main.py`; `/`, `/docs`,
`/openapi.json` and `/static` stay public. There is still no TLS, so tokens cross
the wire in plain HTTP — auth_notes.txt section 6 lists what is left.
