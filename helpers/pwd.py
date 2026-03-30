import bcrypt as _bcrypt


class Hasher():
    @staticmethod
    def verify_password(plain_password: str, hashed_password: str) -> bool:
        return _bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))

    @staticmethod
    def get_password_hash(password: str) -> str:
        return _bcrypt.hashpw(password.encode('utf-8'), _bcrypt.gensalt()).decode('utf-8')
