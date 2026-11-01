"""Authentication: user registration, login, password sessions.

Every connection, conversation, run, and usage record belongs to a user.
Users can only read, update, or delete their own data.
"""
import secrets
import time
from dataclasses import dataclass

import bcrypt

from .config import Config
from .store import Store


SESSION_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days


@dataclass
class CurrentUser:
    id: str
    username: str
    is_authenticated: bool = True


class Auth:
    def __init__(self, store: Store, config: Config):
        self.store = store
        self.config = config

    # -- password hashing -------------------------------------------------

    @staticmethod
    def hash_password(password: str) -> str:
        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    @staticmethod
    def verify_password(password: str, hashed: str) -> bool:
        try:
            return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
        except ValueError:
            return False

    # -- users ------------------------------------------------------------

    def create_user(self, username: str, password: str) -> dict:
        """Create a new user. Raises ValueError if the username exists."""
        return self.store.create_user(username, self.hash_password(password))

    def get_user_by_username(self, username: str) -> dict | None:
        return self.store.get_user_by_username(username)

    def authenticate_user(self, username: str, password: str) -> dict | None:
        user = self.store.get_user_by_username(username)
        if not user:
            return None
        if not self.verify_password(password, user["password_hash"]):
            return None
        return user

    # -- sessions ---------------------------------------------------------

    def create_session(self, user_id: str) -> str:
        """Issue a session token and store it. Returns the raw token."""
        token = secrets.token_urlsafe(32)
        token_hash = self.hash_password(token)  # reuse bcrypt for token hashing
        expires_at = time.time() + SESSION_TTL_SECONDS
        self.store.create_session(user_id, token_hash, expires_at)
        return token

    def get_session(self, token: str) -> dict | None:
        """Validate a session token and return its user, or None."""
        session = self.store.get_session_by_token(token)
        if not session:
            return None
        if time.time() > session["expires_at"]:
            self.store.delete_session(session["id"])
            return None
        user = self.store.get_user(session["user_id"])
        return user if user else None

    def delete_session(self, token: str) -> None:
        session = self.store.get_session_by_token(token)
        if session:
            self.store.delete_session(session["id"])

    def current_user(self, token: str | None) -> CurrentUser | None:
        if not token:
            return None
        user = self.get_session(token)
        if not user:
            return None
        return CurrentUser(id=user["id"], username=user["username"])
