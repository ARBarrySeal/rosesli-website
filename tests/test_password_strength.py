"""Tests for _password_too_weak — per-company policy + common-password block.

rosesli: 7+ chars, 1 capital, 1 number, 1 special (owner-specified).
dod:     12+ chars, 3 of 4 character classes (unchanged).
Both:    common-password block list.
"""
import pytest

from portal_auth import _password_too_weak


@pytest.fixture
def rosesli(monkeypatch):
    monkeypatch.setenv("COMPANY_ID", "rosesli")


@pytest.fixture
def dod(monkeypatch):
    monkeypatch.setenv("COMPANY_ID", "dod")


# ─── rosesli policy ───────────────────────────────────────────────────────────


def test_rosesli_short_password_rejected(rosesli):
    assert "7 characters" in _password_too_weak("Ab1!x2")


def test_rosesli_missing_capital_rejected(rosesli):
    assert "capital" in _password_too_weak("abc123!x")


def test_rosesli_missing_number_rejected(rosesli):
    assert "number" in _password_too_weak("Abcdefg!")


def test_rosesli_missing_special_rejected(rosesli):
    assert "special" in _password_too_weak("Abcd1234")


def test_rosesli_minimal_valid_password_accepted(rosesli):
    assert _password_too_weak("Abc123!") is None


def test_rosesli_default_password_never_passes(rosesli):
    # The account-creation default must always be rejected as a new password.
    assert _password_too_weak("password") is not None


def test_rosesli_common_password_rejected(rosesli):
    msg = _password_too_weak("Password1!")
    assert msg is not None and "too common" in msg


# ─── dod policy (unchanged from wave-2) ───────────────────────────────────────


def test_dod_short_password_rejected(dod):
    assert "12 characters" in _password_too_weak("Abc123!")


def test_dod_three_character_classes_required(dod):
    # 16 chars, all lowercase — only 1 class
    msg = _password_too_weak("abcdefghijklmnop")
    assert msg is not None and "3 of" in msg


def test_dod_strong_unique_password_accepted(dod):
    assert _password_too_weak("J7k$mPq2vNxR9zL!") is None


@pytest.mark.parametrize("pw", [
    # Exact match on common-list entries (when long enough to meet length)
    "Password1234",
    "Welcome12345",
    "P@ssword1234",
    # Suffix-strip catches base words: "Sunshine!" → "sunshine"
    "Sunshine1234",
    "Football2024",
    "Princess9876",
    "Monkey123456",
    "Dragon!@#$%^",
    # The 5 originals
    "Password!!!1",
    "Qwerty123456",
    "Letmein12345",
    "Iloveyou1234",
    "Admin1234!@#",
])
def test_dod_common_password_rejected(dod, pw):
    msg = _password_too_weak(pw)
    assert msg is not None and "too common" in msg, f"expected reject: {pw!r}"


@pytest.mark.parametrize("pw", [
    # Real strong passwords that include common-word fragments should pass
    # IF the base word is not in the list. These are sanity checks.
    "ZyMx7!nQrB2pVk",
    "Tr0ub4dor&3xyz",
    "Correct-Horse-Battery-9",
    "9Lx#pq7zM!nVR2",
])
def test_dod_strong_random_password_passes(dod, pw):
    assert _password_too_weak(pw) is None, f"expected pass: {pw!r}"


def test_dod_common_password_uppercase_variant_still_rejected(dod):
    """Block list is case-insensitive."""
    assert _password_too_weak("PASSWORD1234") is not None


def test_dod_suffix_strip_only_trailing(dod):
    """Common word in middle is OK if base differs after stripping suffix."""
    # "abcpassworddef" — strip trailing nothing → "abcpassworddef", not in list
    assert _password_too_weak("AbcPassword$Def") is None
