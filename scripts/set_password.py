"""Set (or reset) a user's password.

The seeded admin accounts are created without a password on purpose: a
password invented by a migration would either be known to nobody or become a
shared secret in version control. This script is how a real one gets set.

The password is read from a hidden prompt, hashed with bcrypt, and only the
hash is written. It is never echoed, never passed as an argument (where `ps`
would expose it), and never logged.

Usage, from inside the app container:

    docker compose exec app python scripts/set_password.py user@example.com

or in production:

    docker compose -f docker-compose.yml -f docker-compose.prod.yml \
        exec app python scripts/set_password.py user@example.com

IT NEEDS A TERMINAL. The hidden prompt is getpass, which needs a TTY. Run
from an interactive shell ON the droplet, or through `ssh -t`. Over plain
`ssh host "..."`, from a script, or with `exec -T`, there is no TTY and the
prompt fails -- on 2026-09-19 that surfaced as a getpass traceback that read
like a broken script. Without a terminal, use --password-stdin and pipe the
password in from a hidden prompt in YOUR shell:

    read -s PW; printf '%s' "$PW" | docker compose ... exec -T app \
        python scripts/set_password.py user@example.com --password-stdin

It still never appears on a command line or in a log.
"""

import getpass
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
from helpers.pwd import Hasher
from models_pgdb import models

MIN_LENGTH = 12


def _read_password(from_stdin: bool) -> "tuple[str, str | None] | None":
    """(password, confirmation-or-None). None when nothing could be read safely."""
    if from_stdin:
        pw = sys.stdin.readline().rstrip("\r\n")
        return pw, pw
    if not sys.stdin.isatty():
        print("No terminal for the hidden prompt. Run this from an interactive "
              "shell on the host (or `ssh -t`), without `exec -T` -- or pipe the "
              "password in with --password-stdin.")
        return None
    return getpass.getpass("New password: "), None


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if len(args) != 1 or flags - {"--password-stdin"}:
        print(f"usage: python {sys.argv[0]} <email> [--password-stdin]")
        return 2

    email = args[0]
    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.email == email).first()
        if user is None:
            print(f"No user with email {email!r}.")
            return 1

        role = user.role.role if user.role is not None else None
        print(f"Setting password for id={user.id} {user.email} (role: {role or 'none'})")

        got = _read_password("--password-stdin" in flags)
        if got is None:
            return 3
        password, confirm = got
        if len(password) < MIN_LENGTH:
            print(f"Password must be at least {MIN_LENGTH} characters.")
            return 1
        if confirm is None:
            confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords do not match.")
            return 1

        user.password_hash = Hasher.get_password_hash(password)
        db.commit()
        print(f"Password set for {user.email}.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
