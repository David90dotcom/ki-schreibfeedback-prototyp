from __future__ import annotations

import hmac
from collections.abc import Mapping, MutableMapping

from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError


AUTHENTICATED_USER_SESSION_KEY = "authenticated_user"

_PASSWORD_HASHER = PasswordHash.recommended()


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