"""Credential checks for live mode.

Live mode refuses to start without the credentials its venues require, and
says exactly what is missing and how to provide it. No credential check
ever raises a raw traceback: every failure is a :class:`CredentialError`
with a remedy.

- **Kalshi** requires ``KALSHI_API_KEY`` and ``KALSHI_API_SECRET`` (the
  PEM-encoded RSA private key used for RSA-PSS request signing). The PEM is
  parsed here so a malformed key fails before any network call.
- **Polymarket** read endpoints need no credentials.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .venues.base import VenueError


class CredentialError(VenueError):
    """Raised when required credentials are missing or unparsable."""


@dataclass(frozen=True)
class VenueCredentials:
    """Validated credentials for one venue."""

    venue: str
    api_key: str | None = None
    private_key_pem: str | None = None


def check_kalshi_credentials(env: dict | None = None) -> VenueCredentials:
    """Validate Kalshi signing credentials from the environment.

    Raises :class:`CredentialError` listing every missing variable and the
    exact remedy when anything is absent or the PEM does not parse.
    """
    env = os.environ if env is None else env
    api_key = env.get("KALSHI_API_KEY", "").strip()
    api_secret = env.get("KALSHI_API_SECRET", "").strip()

    missing = []
    if not api_key:
        missing.append("KALSHI_API_KEY")
    if not api_secret:
        missing.append("KALSHI_API_SECRET")
    if missing:
        raise CredentialError(
            f"kalshi live mode requires {', '.join(missing)}, which "
            f"{'is' if len(missing) == 1 else 'are'} not set.\n"
            "remedy: create an API key at kalshi.com (Account > API Keys), "
            "then\n"
            "  export KALSHI_API_KEY=<your key id>\n"
            "  export KALSHI_API_SECRET=\"$(cat /path/to/private-key.pem)\"\n"
            "or run without credentials in demo mode "
            "(`pmwatch demo`, fixture data, no network)."
        )

    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError as exc:
        raise CredentialError(
            "kalshi live mode requires the 'cryptography' package for "
            "RSA-PSS request signing.\nremedy: pip install cryptography"
        ) from exc
    try:
        serialization.load_pem_private_key(api_secret.encode(), password=None)
    except ValueError as exc:
        raise CredentialError(
            f"KALSHI_API_SECRET is set but does not parse as a PEM private "
            f"key: {exc}.\nremedy: export the full PEM text, including the "
            "-----BEGIN/END PRIVATE KEY----- lines."
        ) from exc
    return VenueCredentials(
        venue="kalshi", api_key=api_key, private_key_pem=api_secret
    )


def check_live_credentials(venues: list[str], env: dict | None = None) -> dict[str, VenueCredentials | None]:
    """Validate credentials for every venue a pairs file needs.

    Returns one entry per venue (``None`` where no credential is required).
    The first failure raises with a full remedy message.
    """
    creds: dict[str, VenueCredentials | None] = {}
    for venue in venues:
        if venue == "kalshi":
            creds[venue] = check_kalshi_credentials(env)
        elif venue == "polymarket":
            creds[venue] = None  # read endpoints are unauthenticated
        else:
            raise CredentialError(f"no credential policy for venue {venue!r}")
    return creds
