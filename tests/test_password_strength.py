"""Tests for _password_too_weak — common-password block + class rules."""
import pytest

from portal_auth import _password_too_weak


# ─── Length + class rules (unchanged from wave-2) ─────────────────────────────


def test_short_password_rejected():
    assert "12 characters" in _password_too_weak("Abc123!")


def test_three_character_classes_required():
    # 16 chars, all lowercase + digits — only 2 classes
    msg = _password_too_weak("abcdefghijklmnop")
    assert msg is not None and "3 of" in msg


def test_strong_unique_password_accepted():
    assert _password_too_weak("J7k$mPq2vNxR9zL!") is None


# ─── Common-password block ────────────────────────────────────────────────────


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
def test_common_password_rejected(pw):
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
def test_strong_random_password_passes(pw):
    assert _password_too_weak(pw) is None, f"expected pass: {pw!r}"


def test_common_password_uppercase_variant_still_rejected():
    """Block list is case-insensitive."""
    assert _password_too_weak("PASSWORD1234") is not None


def test_suffix_strip_only_trailing():
    """Common word in middle is OK if base differs after stripping suffix."""
    # "abcpassworddef" — strip trailing nothing → "abcpassworddef", not in list
    assert _password_too_weak("AbcPassword$Def") is None
