"""Principal identity value objects used to scope user state.

The browser's anonymous client identifier is treated as a bearer value.  It
is validated at the boundary and represented internally by :class:`Principal`
so callers cannot accidentally use a raw request value as a storage key.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re


class PrincipalValidationError(ValueError):
    """Raised when a client identity is missing or does not meet the contract."""


_CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


@dataclass(frozen=True)
class Principal:
    """A stable identity used to isolate a client's personal state."""

    type: str
    id: str

    @property
    def storage_key(self) -> str:
        """Return the namespaced key used by principal-scoped repositories."""

        return f"{self.type}:{self.id}"

    @property
    def log_digest(self) -> str:
        """Return a short, irreversible identifier suitable for logs.

        The complete storage key is hashed so the raw client ID never appears
        in log correlation fields.  Sixteen hexadecimal characters retain
        enough utility for diagnostics while keeping logs compact.
        """

        return hashlib.sha256(self.storage_key.encode("utf-8")).hexdigest()[:16]


def principal_from_client_id(value: object) -> Principal:
    """Validate and normalize a browser ``client_id``.

    Surrounding whitespace is ignored.  Only the documented URL/storage-safe
    ASCII alphabet is accepted, with a length from 8 through 64 characters.
    """

    if not isinstance(value, str):
        raise PrincipalValidationError("client_id is required")
    client_id = value.strip()
    if not _CLIENT_ID_PATTERN.fullmatch(client_id):
        raise PrincipalValidationError("client_id format is invalid")
    return Principal(type="anonymous_client", id=client_id)


__all__ = ["Principal", "PrincipalValidationError", "principal_from_client_id"]
