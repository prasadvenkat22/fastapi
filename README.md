# Fastapi

Postgres-backed FastAPI service. Set the env file before calling the API endpoints.
See deployment_notes.txt for the stack and strategy_notes.txt for the trading engine.

Accounts: `scripts/add_user.py <email> <role>` creates a user and assigns a role,
then `scripts/set_password.py <email>` sets the password — separate steps, and the
only way to reset a forgotten one. See "Users, roles and passwords" in
deployment_notes.txt.

Note that `/auth/login` issues tokens but no route requires one yet: the
dependencies in `helpers/auth_deps.py` are written and not applied, so the API is
currently open. Same section covers what that means.
