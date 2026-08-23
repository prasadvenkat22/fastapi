import bcrypt as _bcrypt

# One definition, because there were two. scripts/set_password.py required 12
# characters while POST /CRUD/register/ accepted 6, so the weaker rule was the
# one facing the internet. Anything that accepts a password imports this.
MIN_PASSWORD_LENGTH = 12


def password_problem(password: str) -> "str | None":
    """Why this password is unacceptable, or None if it is fine.

    Returns the reason rather than raising, so callers can turn it into a 400,
    a CLI message, or a validation error as suits them.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    # bcrypt silently truncates at 72 BYTES, so a longer passphrase would have
    # its tail ignored -- two different passwords that both authenticate. Told,
    # not truncated.
    if len(password.encode("utf-8")) > 72:
        return "Password must be at most 72 bytes (bcrypt's limit)."
    return None



class Hasher():
    @staticmethod
    def verify_password(plain_password: str, hashed_password: str) -> bool:
        return _bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))

    @staticmethod
    def get_password_hash(password: str) -> str:
        return _bcrypt.hashpw(password.encode('utf-8'), _bcrypt.gensalt()).decode('utf-8')
