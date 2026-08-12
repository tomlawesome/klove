"""Constant-time authentication for Klove's native monitoring API."""

from __future__ import annotations

import hmac
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated local API client."""

    subject: str = "grove"
    scopes: frozenset[str] = frozenset({"printers:read"})


class BearerAuthenticator:
    """Authenticate one configured bearer credential without secret logging."""

    def __init__(self, token: str) -> None:
        """Retain the already validated token in process memory."""
        self._token = token

    def authenticate(self, authorization: str | None) -> Principal | None:
        """Return a principal only for one exact RFC 6750-style header."""
        if authorization is None:
            return None
        scheme, separator, supplied = authorization.partition(" ")
        if separator != " " or scheme != "Bearer" or not supplied or " " in supplied:
            return None
        if not hmac.compare_digest(supplied, self._token):
            return None
        return Principal()


def authorize(principal: Principal | None, required_scope: str) -> bool:
    """Fail closed when identity or an exact required scope is absent."""
    return principal is not None and required_scope in principal.scopes
