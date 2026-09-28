import re

import pytest

from incident_intel.tenancy.api_keys import (
    generate_api_key,
    hash_api_key,
    parse_key_prefix,
    verify_api_key,
)

PEPPER = "p" * 40


def test_generated_key_has_expected_format() -> None:
    key = generate_api_key(PEPPER)

    assert re.fullmatch(r"ii_[a-z0-9]{12}_[A-Za-z0-9_-]{43}", key.plaintext)
    assert parse_key_prefix(key.plaintext) == key.prefix
    assert re.fullmatch(r"[0-9a-f]{64}", key.key_hash)
    assert key.plaintext not in key.key_hash


def test_generated_keys_are_unique() -> None:
    keys = {generate_api_key(PEPPER).plaintext for _ in range(200)}
    prefixes = {parse_key_prefix(key) for key in keys}

    assert len(keys) == 200
    assert len(prefixes) == 200


def test_verify_accepts_correct_key_and_pepper() -> None:
    key = generate_api_key(PEPPER)

    assert verify_api_key(key.plaintext, key.key_hash, PEPPER)


def test_verify_rejects_wrong_pepper() -> None:
    key = generate_api_key(PEPPER)

    assert not verify_api_key(key.plaintext, key.key_hash, "q" * 40)


def test_verify_rejects_tampered_secret() -> None:
    key = generate_api_key(PEPPER)
    last = key.plaintext[-1]
    tampered = key.plaintext[:-1] + ("A" if last != "A" else "B")

    assert not verify_api_key(tampered, key.key_hash, PEPPER)


def test_hash_is_deterministic_for_same_inputs() -> None:
    assert hash_api_key("ii_x", PEPPER) == hash_api_key("ii_x", PEPPER)


@pytest.mark.parametrize(
    "presented",
    [
        "",
        "ii_",
        "ii_abc_def",
        "xx_abcdefghijkl_" + "a" * 43,  # wrong namespace
        "ii_ABCDEFGHIJKL_" + "a" * 43,  # uppercase prefix
        "ii_abcdefghijkl_" + "a" * 42,  # secret too short
        "ii_abcdefghijkl_" + "a" * 44,  # secret too long
        "ii_abcdefghijkl_" + "a" * 42 + "!",  # invalid character
        " ii_abcdefghijkl_" + "a" * 43,  # leading whitespace
    ],
)
def test_parse_rejects_malformed_keys(presented: str) -> None:
    assert parse_key_prefix(presented) is None
