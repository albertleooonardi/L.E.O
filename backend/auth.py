"""Authentication: user registration, login, password sessions.

Every connection, conversation, run, and usage record belongs to a user.
Users can only read, update, or delete their own data.
"""
import hashlib
import secrets
import threading
import time
from dataclasses import dataclass

import bcrypt

from .config import Config
from .store import Store


SESSION_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 10
# Compared against when the username does not exist, so a miss costs as much as a wrong password.
_DUMMY_HASH = bcrypt.hashpw(b"leo-dummy-password", bcrypt.gensalt()).decode("utf-8")


@dataclass
class CurrentUser:
    id: str
    username: str
    is_authenticated: bool = True
    is_admin: bool = False

    @property
    def owner_id(self) -> str | None:
        """The user_id that scopes queries; None (all rows) for the server-token admin."""
        return None if self.id == "system" else self.id


SYSTEM_USER = CurrentUser(id="system", username="system", is_admin=True)


def token_digest(token: str) -> str:
    # Session tokens are 256-bit random values, so a fast hash is enough and allows an indexed lookup.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Auth:
    def __init__(self, store: Store, config: Config):
        self.store = store
        self.config = config
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

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

    def authenticate_user(self, username: str, password: str) -> dict | None:
        user = self.store.get_user_by_username(username)
        ok = self.verify_password(password, user["password_hash"] if user else _DUMMY_HASH)
        return user if user and ok else None

    # -- login throttling -------------------------------------------------

    def login_blocked(self, key: str) -> bool:
        cutoff = time.time() - LOGIN_WINDOW_SECONDS
        with self._lock:
            recent = [t for t in self._failures.get(key, []) if t > cutoff]
            if recent:
                self._failures[key] = recent
            else:
                self._failures.pop(key, None)
            return len(recent) >= LOGIN_MAX_FAILURES

    def record_failure(self, key: str) -> None:
        with self._lock:
            self._failures.setdefault(key, []).append(time.time())

    def clear_failures(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    # -- sessions ---------------------------------------------------------

    def create_session(self, user_id: str) -> str:
        """Issue a session token and store its digest. Returns the raw token."""
        token = secrets.token_urlsafe(32)
        self.store.purge_expired_sessions()
        self.store.create_session(user_id, token_digest(token), time.time() + SESSION_TTL_SECONDS)
        return token

    def delete_session(self, token: str) -> None:
        if token:
            self.store.delete_session_by_digest(token_digest(token))

    def current_user(self, token: str | None) -> CurrentUser | None:
        if not token:
            return None
        session = self.store.get_session_by_digest(token_digest(token))
        user = self.store.get_user(session["user_id"]) if session else None
        if not user:
            return None
        return CurrentUser(id=user["id"], username=user["username"], is_admin=user["id"] == self.store.admin_user_id())
