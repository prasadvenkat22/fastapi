# Fastapi

Postgres-backed FastAPI service. Set the env file before calling the API endpoints.
See deployment_notes.txt for the stack and strategy_notes.txt for the trading engine.

Accounts: `scripts/add_user.py <email> <role>` creates a user and assigns a role,
then `scripts/set_password.py <email>` sets the password — separate steps, and the
only way to reset a forgotten one. The API cannot assign roles, which is
why this is a script — see auth_notes.txt.

Note that `/auth/login` issues tokens but no route requires one yet: the
dependencies in `helpers/auth_deps.py` are written and not applied, so the API is
currently open — including `DELETE /CRUD/users/{id}`. auth_notes.txt section 6
lists what closing that needs.
