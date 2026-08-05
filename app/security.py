from __future__ import annotations

import hmac
import math
import secrets
import time
from collections import deque
from collections.abc import Callable, Mapping, MutableMapping
from threading import Lock

from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError


AUTHENTICATED_USER_SESSION_KEY = "authenticated_user"
CSRF_TOKEN_SESSION_KEY = "csrf_token"

_PASSWORD_HASHER = PasswordHash.recommended()


def get_or_create_csrf_token(
    session: MutableMapping[str, object],
) -> str:
    """Liefert das CSRF-Token der Sitzung oder erzeugt eines."""
    token = session.get(CSRF_TOKEN_SESSION_KEY)

    if isinstance(token, str) and token:
        return token

    token = secrets.token_urlsafe(32)
    session[CSRF_TOKEN_SESSION_KEY] = token

    return token


def is_valid_csrf_token(
    session: Mapping[str, object],
    submitted_token: str | None,
) -> bool:
    """Vergleicht Formular- und Sitzungstoken sicher."""
    expected_token = session.get(CSRF_TOKEN_SESSION_KEY)

    if (
        not isinstance(expected_token, str)
        or not expected_token
        or not isinstance(submitted_token, str)
        or not submitted_token
    ):
        return False

    return hmac.compare_digest(
        submitted_token.encode("utf-8"),
        expected_token.encode("utf-8"),
    )


class LoginRateLimiter:
    """Begrenzt fehlgeschlagene Logins je Client und Prozess."""

    def __init__(
        self,
        *,
        max_attempts: int,
        window_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_attempts <= 0:
            raise ValueError("max_attempts muss positiv sein.")

        if window_seconds <= 0:
            raise ValueError("window_seconds muss positiv sein.")

        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._clock = clock
        self._failed_attempts: dict[str, deque[float]] = {}
        self._lock = Lock()

    def retry_after_seconds(
        self,
        client_key: str,
    ) -> int | None:
        """Liefert die Sperrzeit oder None für einen freien Versuch."""
        now = self._clock()

        with self._lock:
            attempts = self._active_attempts(
                client_key,
                now,
            )

            if len(attempts) < self._max_attempts:
                return None

            retry_after = self._window_seconds - (
                now - attempts[0]
            )

            return max(1, math.ceil(retry_after))

    def record_failure(self, client_key: str) -> None:
        """Merkt einen fehlgeschlagenen Anmeldeversuch."""
        now = self._clock()

        with self._lock:
            attempts = self._active_attempts(
                client_key,
                now,
            )

            if client_key not in self._failed_attempts:
                self._failed_attempts[client_key] = attempts

            attempts.append(now)

    def reset(self, client_key: str) -> None:
        """Entfernt die Fehlversuche nach erfolgreichem Login."""
        with self._lock:
            self._failed_attempts.pop(client_key, None)

    def _active_attempts(
        self,
        client_key: str,
        now: float,
    ) -> deque[float]:
        attempts = self._failed_attempts.get(
            client_key,
            deque(),
        )
        cutoff = now - self._window_seconds

        while attempts and attempts[0] <= cutoff:
            attempts.popleft()

        if not attempts:
            self._failed_attempts.pop(client_key, None)

        return attempts


def verify_credentials(
    *,
    username: str,
    password: str,
    expected_username: str,
    password_hash: str | None,
) -> bool:
    """Prüft Benutzername und Passwort ohne Klartextspeicherung."""
    username_matches = hmac.compare_digest(
        username.encode("utf-8"),
        expected_username.encode("utf-8"),
    )

    password_matches = False

    if password_hash:
        try:
            password_matches = _PASSWORD_HASHER.verify(
                password,
                password_hash,
            )
        except (TypeError, ValueError, UnknownHashError):
            password_matches = False

    return username_matches and password_matches


def start_authenticated_session(
    session: MutableMapping[str, object],
    username: str,
) -> None:
    """Ersetzt alte Sitzungsdaten durch den angemeldeten Benutzer."""
    session.clear()
    session[AUTHENTICATED_USER_SESSION_KEY] = username


def authenticated_username(
    session: Mapping[str, object],
) -> str | None:
    """Liest einen gültigen Benutzernamen aus der Sitzung."""
    username = session.get(AUTHENTICATED_USER_SESSION_KEY)

    if not isinstance(username, str) or not username:
        return None

    return username


def is_authenticated(
    session: Mapping[str, object],
    expected_username: str,
) -> bool:
    """Prüft, ob die Sitzung zum konfigurierten Konto gehört."""
    username = authenticated_username(session)

    if username is None:
        return False

    return hmac.compare_digest(
        username.encode("utf-8"),
        expected_username.encode("utf-8"),
    )


def end_authenticated_session(
    session: MutableMapping[str, object],
) -> None:
    """Entfernt sämtliche Daten der aktuellen Sitzung."""
    session.clear()
