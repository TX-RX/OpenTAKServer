"""Unit tests for the Mumble authenticator's identity resolution.

These cover the presented-username normalization that keeps ATAK Vx plugin
clients (which connect as ``callsign---<uid>``) matching their OTS account,
plus the Mumble id/display-name mapping.

The authenticator module loads Murmur's Ice slice at import time, so the whole
module is unavailable without Ice installed -- skip cleanly in that case (the
suite runs under CI and on the server, where Ice is present).
"""
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("Ice")

from opentakserver.mumble.mumble_authenticator import (  # noqa: E402
    MUMBLE_ID_RANGE,
    MumbleAuthenticator,
)


# --------------------------------------------------------------------------- #
# _candidate_callsigns -- pure presented-name normalization
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "username,expected",
    [
        # plain PC username -- nothing to strip
        ("plainuser", ["plainuser"]),
        # ATAK Vx suffix stripped -> base callsign added
        (
            "ANVIL---47c4c853-4e52-4b97-9a0b-08a0f961b0fa",
            ["ANVIL---47c4c853-4e52-4b97-9a0b-08a0f961b0fa", "ANVIL"],
        ),
        # underscores in a Vx callsign map back to spaces
        ("Some_User---uid", ["Some_User---uid", "Some_User", "Some User"]),
        # underscore->space also applies without a suffix
        ("Some_User", ["Some_User", "Some User"]),
        # stray whitespace around the callsign portion is trimmed (the hardening
        # borrowed from upstream PR #338) so it still matches TRIM()'d rows
        ("  ANVIL ---uid", ["  ANVIL ---uid", "ANVIL"]),
        # empty suffix still yields the base callsign, no empty candidate
        ("ANVIL---", ["ANVIL---", "ANVIL"]),
    ],
)
def test_candidate_callsigns(username, expected):
    assert MumbleAuthenticator._candidate_callsigns(username) == expected


def test_candidate_callsigns_has_no_duplicates_or_empties():
    for username in ("bob", "bob---uid", "a_b---uid", "  x ---y", "---"):
        candidates = MumbleAuthenticator._candidate_callsigns(username)
        assert all(candidates), f"empty candidate for {username!r}: {candidates}"
        assert len(candidates) == len(set(candidates)), (
            f"duplicate candidate for {username!r}: {candidates}"
        )


# --------------------------------------------------------------------------- #
# mumble_identity -- id / display-name mapping
# --------------------------------------------------------------------------- #
def test_mumble_identity_pc_user_uses_base_id_and_username():
    user = SimpleNamespace(id=5, username="bob")
    mumble_id, display = MumbleAuthenticator.mumble_identity(user, False, "bob")
    assert mumble_id == 5 * MUMBLE_ID_RANGE
    assert display == "bob"


def test_mumble_identity_callsign_is_deterministic_and_within_user_range():
    user = SimpleNamespace(id=5, username="bob")
    base = 5 * MUMBLE_ID_RANGE

    first, display = MumbleAuthenticator.mumble_identity(user, True, "ANVIL")
    again, _ = MumbleAuthenticator.mumble_identity(user, True, "ANVIL")

    assert display == "ANVIL"  # callsign is the display name, not the username
    assert first == again  # deterministic offset for a given callsign
    assert base < first < base + MUMBLE_ID_RANGE  # stays inside the user's block

    # a different callsign lands on a different id (so one account can host
    # multiple simultaneous device connections)
    other, _ = MumbleAuthenticator.mumble_identity(user, True, "HAMMER")
    assert other != first


# --------------------------------------------------------------------------- #
# resolve_identity -- lookup chain wiring (EUD queries mocked out)
# --------------------------------------------------------------------------- #
def _fake_eud_module(first_returns):
    """Fake ``opentakserver.models.EUD`` whose EUD.query.filter().first()
    yields the given values in order (one per candidate query)."""
    fake_eud_cls = MagicMock()
    fake_eud_cls.query.filter.return_value.first.side_effect = list(first_returns)
    return SimpleNamespace(EUD=fake_eud_cls)


def _app(username_lookup=None, id_lookup=None):
    app = MagicMock()

    def find_user(**kwargs):
        if "username" in kwargs:
            return username_lookup
        if "id" in kwargs:
            return id_lookup
        return None

    app.security.datastore.find_user.side_effect = find_user
    return app


def _patched(fake_module):
    """Replace the inline EUD import and the sqlalchemy ``func`` so the lookup
    chain runs without a database."""
    return (
        patch.dict(sys.modules, {"opentakserver.models.EUD": fake_module}),
        patch("opentakserver.mumble.mumble_authenticator.func"),
    )


def test_resolve_identity_returns_ots_user_without_callsign_auth():
    ots_user = SimpleNamespace(id=1, username="bob")
    app = _app(username_lookup=ots_user)
    mods, func = _patched(_fake_eud_module([]))
    with mods, func:
        user, is_callsign_auth = MumbleAuthenticator.resolve_identity(app, "bob")
    assert user is ots_user
    assert is_callsign_auth is False


def test_resolve_identity_matches_eud_by_stripped_callsign():
    eud = SimpleNamespace(user_id=42)
    resolved = SimpleNamespace(id=42, username="anvil-owner")
    app = _app(username_lookup=None, id_lookup=resolved)
    # candidates for "ANVIL---<uid>" are [full, "ANVIL"]; miss the full, hit base
    mods, func = _patched(_fake_eud_module([None, eud]))
    with mods, func:
        user, is_callsign_auth = MumbleAuthenticator.resolve_identity(
            app, "ANVIL---47c4c853-4e52-4b97-9a0b-08a0f961b0fa"
        )
    assert user is resolved
    assert is_callsign_auth is True


def test_resolve_identity_returns_none_when_nothing_matches():
    app = _app(username_lookup=None, id_lookup=None)
    mods, func = _patched(_fake_eud_module([None, None, None]))
    with mods, func:
        user, is_callsign_auth = MumbleAuthenticator.resolve_identity(
            app, "ghost", certlist=None
        )
    assert user is None
    assert is_callsign_auth is False
