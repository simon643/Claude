"""Signing in to Microsoft, with the device authorization grant.

Device code rather than authorization-code-with-a-secret, for the same reason
the sibling Xero app uses PKCE: a desktop application cannot keep a secret.
Device code goes further and needs no redirect URI and no local listener at
all — you get a short code, you type it into microsoft.com/devicelogin in
whatever browser you are already signed in to, and this process polls until
that finishes. The app registration must have "Allow public client flows"
enabled; there is nothing else to configure.

The refresh token rotates. It is written to disk before the new access token is
handed back, so a crash mid-refresh cannot strand you holding a token Microsoft
has already invalidated.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import (
    MS_AUTHORITY,
    TEAMS_MAIL_SCOPE,
    TEAMS_RECORDING_SCOPE,
    TEAMS_SCOPES,
    ensure_dirs,
    teams_token_path,
)
from . import TeamsAuthError, TeamsError
from .transport import Response, Transport, form, urllib_transport

DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
# Refresh this far before nominal expiry, so a slow request cannot land on the
# far side of the boundary with a token that expired in flight.
EXPIRY_SKEW = 300
# Give up on a sign-in nobody is completing. Microsoft's own device codes
# expire in about 15 minutes.
MAX_POLL_SECONDS = 15 * 60


@dataclass
class DeviceCode:
    """What the user needs in order to approve the sign-in."""

    device_code: str
    user_code: str
    verification_uri: str
    message: str = ""
    interval: int = 5
    expires_in: int = 900


@dataclass
class TokenSet:
    access_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0
    scopes: list[str] = field(default_factory=list)
    account: str = ""
    tenant: str = "organizations"
    client_id: str = ""

    @property
    def is_expired(self) -> bool:
        return time.time() >= (self.expires_at - EXPIRY_SKEW)

    def _granted(self, scope: str) -> bool:
        # Graph echoes scopes back fully qualified
        # ("https://graph.microsoft.com/Mail.Send"), so compare on the suffix.
        needle = scope.lower()
        return any(granted.lower().endswith(needle) for granted in self.scopes)

    @property
    def can_read_recordings(self) -> bool:
        return self._granted(TEAMS_RECORDING_SCOPE)

    @property
    def can_send_mail(self) -> bool:
        return self._granted(TEAMS_MAIL_SCOPE)

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scopes": self.scopes,
            "account": self.account,
            "tenant": self.tenant,
            "client_id": self.client_id,
        }

    def save(self, path: Path | None = None) -> Path:
        ensure_dirs()
        target = path or teams_token_path()
        target.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        # A refresh token is a bearer credential for someone's whole calendar.
        with suppress(OSError):
            target.chmod(0o600)
        return target

    @classmethod
    def load(cls, path: Path | None = None) -> TokenSet | None:
        target = path or teams_token_path()
        if not target.exists():
            return None
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(raw, dict):
            return None
        return cls(
            access_token=str(raw.get("access_token", "")),
            refresh_token=str(raw.get("refresh_token", "")),
            expires_at=float(raw.get("expires_at", 0.0) or 0.0),
            scopes=[str(s) for s in raw.get("scopes", [])],
            account=str(raw.get("account", "")),
            tenant=str(raw.get("tenant", "organizations")),
            client_id=str(raw.get("client_id", "")),
        )


class TeamsAuth:
    """Holds the sign-in, and hands out access tokens."""

    def __init__(
        self,
        client_id: str,
        tenant: str = "organizations",
        *,
        with_recordings: bool = False,
        with_email: bool = False,
        transport: Transport = urllib_transport,
        token_path: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client_id = client_id.strip()
        self.tenant = (tenant or "organizations").strip()
        self.scopes = list(TEAMS_SCOPES)
        if with_recordings:
            self.scopes.append(TEAMS_RECORDING_SCOPE)
        if with_email:
            self.scopes.append(TEAMS_MAIL_SCOPE)
        self._transport = transport
        self._token_path = token_path
        self._sleep = sleep
        self._clock = clock
        self._tokens: TokenSet | None = None

    # -- state ------------------------------------------------------------

    @property
    def tokens(self) -> TokenSet | None:
        if self._tokens is None:
            self._tokens = TokenSet.load(self._token_path)
        return self._tokens

    @property
    def signed_in(self) -> bool:
        tokens = self.tokens
        return bool(tokens and (tokens.access_token or tokens.refresh_token))

    @property
    def can_send_mail(self) -> bool:
        """Whether the sign-in actually carries the Mail.Send permission."""
        tokens = self.tokens
        return bool(tokens and tokens.can_send_mail)

    @property
    def can_read_recordings(self) -> bool:
        tokens = self.tokens
        return bool(tokens and tokens.can_read_recordings)

    def logout(self) -> bool:
        """Forget the local sign-in. Nothing is revoked server-side."""
        target = self._token_path or teams_token_path()
        self._tokens = None
        if target.exists():
            target.unlink()
            return True
        return False

    # -- sign-in ----------------------------------------------------------

    def begin(self) -> DeviceCode:
        if not self.client_id:
            raise TeamsAuthError(
                "no Microsoft application id configured — register an app (see the README) "
                "then run: minutely config --teams-client-id <id>, "
                "or set MINUTELY_TEAMS_CLIENT_ID"
            )
        response = self._transport(
            "POST",
            f"{MS_AUTHORITY}/{self.tenant}/oauth2/v2.0/devicecode",
            {"Content-Type": "application/x-www-form-urlencoded"},
            form({"client_id": self.client_id, "scope": " ".join(self.scopes)}),
        )
        if not response.ok:
            raise TeamsAuthError(f"Microsoft refused the sign-in request: {_describe(response)}")
        payload = response.json()
        return DeviceCode(
            device_code=str(payload.get("device_code", "")),
            user_code=str(payload.get("user_code", "")),
            verification_uri=str(payload.get("verification_uri", "")),
            message=str(payload.get("message", "")),
            interval=int(payload.get("interval", 5) or 5),
            expires_in=int(payload.get("expires_in", 900) or 900),
        )

    def poll(self, code: DeviceCode) -> TokenSet:
        """Wait for the user to approve the sign-in, then store the tokens."""
        interval = max(1, code.interval)
        deadline = self._clock() + min(code.expires_in, MAX_POLL_SECONDS)
        while True:
            if self._clock() >= deadline:
                raise TeamsAuthError("the sign-in code expired before it was approved")
            self._sleep(interval)
            response = self._transport(
                "POST",
                f"{MS_AUTHORITY}/{self.tenant}/oauth2/v2.0/token",
                {"Content-Type": "application/x-www-form-urlencoded"},
                form(
                    {
                        "grant_type": DEVICE_CODE_GRANT,
                        "client_id": self.client_id,
                        "device_code": code.device_code,
                    }
                ),
            )
            if response.ok:
                return self._store(response.json())

            error = str(response.json().get("error", ""))
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                # The spec is explicit: back off by five seconds and keep the
                # longer interval for every subsequent poll.
                interval += 5
                continue
            if error == "authorization_declined":
                raise TeamsAuthError("the sign-in was declined")
            if error == "expired_token":
                raise TeamsAuthError("the sign-in code expired before it was approved")
            raise TeamsAuthError(f"sign-in failed: {_describe(response)}")

    def login(self, announce: Callable[[DeviceCode], None] | None = None) -> TokenSet:
        code = self.begin()
        if announce is not None:
            announce(code)
        return self.poll(code)

    # -- tokens -----------------------------------------------------------

    def access_token(self) -> str:
        """A usable access token, refreshing first if it is close to expiry."""
        tokens = self.tokens
        if tokens is None:
            raise TeamsAuthError("not signed in to Microsoft — run: minutely teams login")
        if tokens.access_token and not tokens.is_expired:
            return tokens.access_token
        if not tokens.refresh_token:
            raise TeamsAuthError("the Microsoft sign-in has expired — run: minutely teams login")
        return self.refresh().access_token

    def refresh(self) -> TokenSet:
        tokens = self.tokens
        if tokens is None or not tokens.refresh_token:
            raise TeamsAuthError("not signed in to Microsoft — run: minutely teams login")
        response = self._transport(
            "POST",
            f"{MS_AUTHORITY}/{tokens.tenant or self.tenant}/oauth2/v2.0/token",
            {"Content-Type": "application/x-www-form-urlencoded"},
            form(
                {
                    "grant_type": "refresh_token",
                    "client_id": tokens.client_id or self.client_id,
                    "refresh_token": tokens.refresh_token,
                    "scope": " ".join(tokens.scopes or self.scopes),
                }
            ),
        )
        if not response.ok:
            error = str(response.json().get("error", ""))
            if error in {"invalid_grant", "invalid_client", "unauthorized_client"}:
                raise TeamsAuthError(
                    "the Microsoft sign-in is no longer valid (password change, revoked consent, "
                    "or conditional access) — run: minutely teams login"
                )
            raise TeamsAuthError(f"could not refresh the Microsoft sign-in: {_describe(response)}")
        return self._store(response.json())

    def _store(self, payload: dict[str, Any]) -> TokenSet:
        previous = self.tokens
        granted = str(payload.get("scope", "")).split()
        tokens = TokenSet(
            access_token=str(payload.get("access_token", "")),
            # A refresh response that omits a new refresh token means keep the
            # old one; treating that as "signed out" would be a bug you only
            # notice an hour later.
            refresh_token=str(payload.get("refresh_token", ""))
            or (previous.refresh_token if previous else ""),
            expires_at=self._clock() + float(payload.get("expires_in", 3600) or 3600),
            scopes=granted or (previous.scopes if previous else self.scopes),
            account=_account_from(payload) or (previous.account if previous else ""),
            tenant=self.tenant or (previous.tenant if previous else "organizations"),
            client_id=self.client_id or (previous.client_id if previous else ""),
        )
        if not tokens.access_token:
            raise TeamsAuthError("Microsoft returned no access token")
        # Written before the token is used, so an interrupted run cannot lose a
        # rotated refresh token.
        tokens.save(self._token_path)
        self._tokens = tokens
        return tokens


def _account_from(payload: dict[str, Any]) -> str:
    """Read the signed-in account out of the id token, if one came back.

    The JWT is not verified: this is a label for the status line, and the
    access token's authority is Microsoft's to judge, not ours.
    """
    raw = str(payload.get("id_token", ""))
    parts = raw.split(".")
    if len(parts) != 3:
        return ""
    import base64

    body = parts[1]
    padding = "=" * (-len(body) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(body + padding))
    except (ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(claims, dict):
        return ""
    for key in ("preferred_username", "upn", "email", "name"):
        value = claims.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _describe(response: Response) -> str:
    payload = {}
    with suppress(TeamsError):
        payload = response.json()
    description = str(payload.get("error_description", "")).splitlines()
    if description:
        return description[0]
    return str(payload.get("error", "")) or f"HTTP {response.status}"
