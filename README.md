# Fastapi

Postgres-backed FastAPI service. Set the env file before calling the API endpoints.
See deployment_notes.txt for the stack and strategy_notes.txt for the trading engine.

Accounts: admins manage users at `/users` (create with a role, change role, disable,
delete, reset a forgotten password) using a token from `POST /auth/login`. Users
change their own password at `POST /auth/change-password`, or recover a
forgotten one at `POST /auth/forgot-password` (needs SMTP configured). `scripts/add_user.py` and
`scripts/set_password.py` do the same against the database directly, and are the way
back when nobody can log in. See auth_notes.txt.

Every router except `/auth` requires a token, applied in `main.py`; `/`, `/docs`,
`/openapi.json` and `/static` stay public. There is still no TLS, so tokens cross
the wire in plain HTTP — auth_notes.txt section 6 lists what is left.
