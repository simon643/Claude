"""Signing in to Microsoft, without Microsoft."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest

from minutely.teams import TeamsAuthError
from minutely.teams.auth import TeamsAuth, TokenSet
from tests.fakes import FakeMicrosoft, json_response, signed_in_token

CLIENT_ID = "11111111-2222-3333-4444-555555555555"


def id_token(claims: dict[str, str]) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


def make_auth(fake: FakeMicrosoft, tmp_path: Path, **kwargs: object) -> TeamsAuth:
    return TeamsAuth(
        client_id=str(kwargs.pop("client_id", CLIENT_ID)),
        transport=fake,
        token_path=tmp_path / "teams-token.json",
        sleep=lambda _seconds: None,
        **kwargs,  # type: ignore[arg-type]
    )


def device_code_response() -> object:
    return json_response(
        {
            "device_code": "device-code-value",
            "user_code": "H7XK2M9P",
            "verification_uri": "https://microsoft.com/devicelogin",
            "message": "To sign in, use a web browser to open https://microsoft.com/devicelogin",
            "interval": 5,
            "expires_in": 900,
        }
    )


def token_response(**overrides: object) -> object:
    payload = {
        "access_token": "access-token-value",
        "refresh_token": "refresh-token-value",
        "expires_in": 3600,
        "scope": "Calendars.Read OnlineMeetingTranscript.Read.All",
        "id_token": id_token({"preferred_username": "dana@example.com"}),
    }
    payload.update(overrides)  # type: ignore[arg-type]
    return json_response(payload)


# -- starting a sign-in -----------------------------------------------------


def test_begin_asks_for_a_device_code_with_the_configured_scopes(tmp_path: Path) -> None:
    fake = FakeMicrosoft().add(r"/devicecode", device_code_response())
    code = make_auth(fake, tmp_path).begin()

    assert code.user_code == "H7XK2M9P"
    assert code.verification_uri == "https://microsoft.com/devicelogin"
    requested = fake.calls[0].form
    assert requested["client_id"] == CLIENT_ID
    assert "OnlineMeetingTranscript.Read.All" in requested["scope"]
    assert "offline_access" in requested["scope"]
    # Least privilege: reading transcripts does not need access to recordings.
    assert "OnlineMeetingRecording.Read.All" not in requested["scope"]


def test_recording_access_is_opt_in(tmp_path: Path) -> None:
    fake = FakeMicrosoft().add(r"/devicecode", device_code_response())
    make_auth(fake, tmp_path, with_recordings=True).begin()
    assert "OnlineMeetingRecording.Read.All" in fake.calls[0].form["scope"]


def test_signing_in_without_an_application_id_says_what_to_do(tmp_path: Path) -> None:
    auth = make_auth(FakeMicrosoft(), tmp_path, client_id="")
    with pytest.raises(TeamsAuthError, match="teams-client-id"):
        auth.begin()


# -- polling ----------------------------------------------------------------


def test_polling_waits_for_approval_then_stores_the_tokens(tmp_path: Path) -> None:
    fake = (
        FakeMicrosoft()
        .add(r"/devicecode", device_code_response())
        .add(
            r"/oauth2/v2.0/token",
            json_response({"error": "authorization_pending"}, 400),
            json_response({"error": "authorization_pending"}, 400),
            token_response(),
        )
    )
    auth = make_auth(fake, tmp_path)
    tokens = auth.login()

    assert tokens.access_token == "access-token-value"
    assert tokens.account == "dana@example.com"
    token_file = tmp_path / "teams-token.json"
    assert token_file.exists()
    # A refresh token is a bearer credential for a calendar; keep it private.
    assert token_file.stat().st_mode & 0o077 == 0
    assert json.loads(token_file.read_text())["refresh_token"] == "refresh-token-value"


def test_slow_down_backs_the_polling_off(tmp_path: Path) -> None:
    waits: list[float] = []
    fake = (
        FakeMicrosoft()
        .add(r"/devicecode", device_code_response())
        .add(
            r"/oauth2/v2.0/token",
            json_response({"error": "slow_down"}, 400),
            token_response(),
        )
    )
    auth = TeamsAuth(
        client_id=CLIENT_ID,
        transport=fake,
        token_path=tmp_path / "t.json",
        sleep=waits.append,
    )
    auth.login()
    assert waits == [5, 10]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("authorization_declined", "declined"),
        ("expired_token", "expired"),
        ("bad_verification_code", "sign-in failed"),
    ],
)
def test_polling_stops_on_a_terminal_error(tmp_path: Path, error: str, expected: str) -> None:
    fake = (
        FakeMicrosoft()
        .add(r"/devicecode", device_code_response())
        .add(r"/oauth2/v2.0/token", json_response({"error": error}, 400))
    )
    with pytest.raises(TeamsAuthError, match=expected):
        make_auth(fake, tmp_path).login()


def test_polling_gives_up_when_the_code_expires(tmp_path: Path) -> None:
    fake = (
        FakeMicrosoft()
        .add(r"/devicecode", device_code_response())
        .add(r"/oauth2/v2.0/token", json_response({"error": "authorization_pending"}, 400))
    )
    clock = iter([0.0, 0.0, 10_000.0, 10_000.0])
    auth = TeamsAuth(
        client_id=CLIENT_ID,
        transport=fake,
        token_path=tmp_path / "t.json",
        sleep=lambda _s: None,
        clock=lambda: next(clock),
    )
    with pytest.raises(TeamsAuthError, match="expired"):
        auth.login()


# -- using and refreshing tokens -------------------------------------------


def test_a_live_token_is_used_as_is(tmp_path: Path) -> None:
    signed_in_token(tmp_path / "teams-token.json")
    fake = FakeMicrosoft()
    assert make_auth(fake, tmp_path).access_token() == "access-token-value"
    assert fake.calls == []


def test_an_expired_token_is_refreshed(tmp_path: Path) -> None:
    signed_in_token(tmp_path / "teams-token.json", expires_at=time.time() - 10)
    fake = FakeMicrosoft().add(r"/oauth2/v2.0/token", token_response(access_token="fresh-token"))
    assert make_auth(fake, tmp_path).access_token() == "fresh-token"
    assert fake.calls[0].form["grant_type"] == "refresh_token"
    assert fake.calls[0].form["refresh_token"] == "refresh-token-value"


def test_a_refresh_without_a_new_refresh_token_keeps_the_old_one(tmp_path: Path) -> None:
    path = signed_in_token(tmp_path / "teams-token.json", expires_at=time.time() - 10)
    fake = FakeMicrosoft().add(
        r"/oauth2/v2.0/token", token_response(refresh_token="", access_token="fresh")
    )
    make_auth(fake, tmp_path).access_token()
    assert json.loads(path.read_text())["refresh_token"] == "refresh-token-value"


def test_a_rotated_refresh_token_is_written_before_it_is_used(tmp_path: Path) -> None:
    path = signed_in_token(tmp_path / "teams-token.json", expires_at=time.time() - 10)
    fake = FakeMicrosoft().add(r"/oauth2/v2.0/token", token_response(refresh_token="rotated"))
    auth = make_auth(fake, tmp_path)
    auth.access_token()
    assert json.loads(path.read_text())["refresh_token"] == "rotated"


def test_a_dead_refresh_token_tells_the_user_to_sign_in_again(tmp_path: Path) -> None:
    signed_in_token(tmp_path / "teams-token.json", expires_at=time.time() - 10)
    fake = FakeMicrosoft().add(
        r"/oauth2/v2.0/token",
        json_response({"error": "invalid_grant", "error_description": "AADSTS700082: expired"}, 400),
    )
    with pytest.raises(TeamsAuthError, match="minutely teams login"):
        make_auth(fake, tmp_path).access_token()


def test_not_signed_in_at_all(tmp_path: Path) -> None:
    auth = make_auth(FakeMicrosoft(), tmp_path)
    assert auth.signed_in is False
    with pytest.raises(TeamsAuthError, match="not signed in"):
        auth.access_token()


def test_logout_forgets_the_tokens(tmp_path: Path) -> None:
    path = signed_in_token(tmp_path / "teams-token.json")
    auth = make_auth(FakeMicrosoft(), tmp_path)
    assert auth.signed_in is True
    assert auth.logout() is True
    assert not path.exists()
    assert auth.signed_in is False
    assert auth.logout() is False


def test_recording_scope_is_visible_on_the_token(tmp_path: Path) -> None:
    signed_in_token(
        tmp_path / "teams-token.json",
        scopes=["https://graph.microsoft.com/OnlineMeetingRecording.Read.All"],
    )
    tokens = TokenSet.load(tmp_path / "teams-token.json")
    assert tokens is not None and tokens.can_read_recordings is True


def test_a_corrupt_token_file_is_treated_as_signed_out(tmp_path: Path) -> None:
    path = tmp_path / "teams-token.json"
    path.write_text("{ not json")
    assert TokenSet.load(path) is None
