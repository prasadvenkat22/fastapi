"""Create a user and give them a role — the step before set_password.py.

set_password.py assumed the account already existed, because the first two
admins were inserted by hand. That does not scale to a third, and hand-written
SQL is how a user ends up with a role_id pointing at nothing or an email that
differs from the login by a capital letter.

Nothing here sets a password. The two are deliberately separate commands: this
one is safe to run over SSH in a shared terminal, and set_password.py is the
one that must be run at a keyboard the person owns. A password invented here
would have to be transmitted somehow, and every way of doing that is worse
than the account simply not having one until its owner sets it.

An account without a password cannot log in — login rejects a null hash rather
than treating it as empty. Creating the row early is therefore harmless.

Usage, from inside the app container:

    docker compose exec app python scripts/add_user.py someone@example.com admin

or in production:

    docker compose -f docker-compose.yml -f docker-compose.prod.yml \
        exec app python scripts/add_user.py someone@example.com admin

Re-running with a different role changes the role and leaves the password
alone, which is how a promotion or demotion happens.
"""

import os
import sys

# The repository root, so these scripts run the same way from anywhere.
# Python puts the SCRIPT's directory on sys.path, not the working directory,
# so `python scripts/x.py` from /app cannot import config or models_pgdb. The
# cron job hides this by exporting PYTHONPATH=/app in its own invocation,
# which means the usage documented above -- a human at a shell -- was the one
# path nobody had run.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.db_pgrs import SessionLocal
from models_pgdb import models


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print(f"usage: python {sys.argv[0]} <email> [role]")
        print("  role defaults to 'user'; see the roles table for what exists.")
        return 2

    # Emails are compared exactly at login, so normalise here rather than
    # discovering later that Venkat@ and venkat@ are two accounts.
    email = sys.argv[1].strip().lower()
    role_name = (sys.argv[2] if len(sys.argv) == 3 else "user").strip().lower()

    db = SessionLocal()
    try:
        role = db.query(models.Role).filter(models.Role.role == role_name).first()
        if role is None:
            have = ", ".join(r.role for r in db.query(models.Role).all()) or "none"
            print(f"No role named {role_name!r}. Roles that exist: {have}.")
            return 1

        user = db.query(models.User).filter(models.User.email == email).first()
        if user is not None:
            was = user.role.role if user.role is not None else None
            if was == role_name:
                print(f"{email} already exists (id={user.id}) with role {role_name}. Nothing to do.")
                return 0
            user.role_id = role.id
            db.commit()
            print(f"{email} (id={user.id}) role changed: {was or 'none'} -> {role_name}.")
            return 0

        # name is what /auth/me returns and what a UI would greet them by. The
        # local part is a placeholder, not an identity claim.
        user = models.User(name=email.split("@")[0], email=email, role_id=role.id)
        db.add(user)
        db.commit()
        print(f"Created {email} (id={user.id}) with role {role_name}, no password.")
        print(f"They cannot log in until: python scripts/set_password.py {email}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
