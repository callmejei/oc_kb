"""Local login: scrypt hashes, JWT issue/verify, rejection of expired, forged and 'none' tokens."""

from __future__ import annotations

import json
import time

import jwt
import pytest

from kb.gateway.auth import (
    ALGORITHM,
    AUDIENCE,
    ISSUER,
    AuthError,
    LocalPasswordStore,
    TokenIssuer,
    generate_local_users,
    hash_password,
    verify_password,
)

SECRET = "s" * 48


def test_password_hash_roundtrip_and_salting():
    h1, h2 = hash_password("pw"), hash_password("pw")
    assert h1 != h2 and verify_password("pw", h1) and not verify_password("PW", h1)
    assert not verify_password("pw", "md5$x$y") and not verify_password("pw", "garbage")


def test_generate_users_writes_hashes_not_plaintext_to_gateway_file(tmp_path):
    new = generate_local_users(["a", "b"], tmp_path)
    hashes = json.loads((tmp_path / "local_users.hash.json").read_text())
    assert set(new) == {"a", "b"} and all(v.startswith("scrypt$") for v in hashes.values())
    assert new["a"] not in (tmp_path / "local_users.hash.json").read_text()
    assert generate_local_users(["a", "b"], tmp_path) == {}  # idempotent: passwords are not rotated silently
    store = LocalPasswordStore(tmp_path / "local_users.hash.json")
    assert store.check("a", new["a"]) and not store.check("a", new["b"]) and not store.check("zz", new["a"])


def test_token_roundtrip():
    iss = TokenIssuer(SECRET, 5)
    tok = iss.issue("ret.user1")
    assert tok["expires_in"] == 300 and iss.verify(tok["access_token"]) == "ret.user1"


def _forge(claims, secret=SECRET, alg=ALGORITHM):  # type: ignore[no-untyped-def]
    base = {"sub": "ret.user1", "iat": int(time.time()), "exp": int(time.time()) + 60, "iss": ISSUER, "aud": AUDIENCE}
    return jwt.encode({**base, **claims}, secret, algorithm=alg)


@pytest.mark.parametrize(
    "token",
    [
        _forge({"exp": int(time.time()) - 10}),  # expired
        _forge({}, secret="x" * 48),  # wrong key
        _forge({"aud": "other"}),  # wrong audience
        _forge({"iss": "evil"}),  # wrong issuer
        jwt.encode(
            {"sub": "ret.user1", "exp": int(time.time()) + 60, "iss": ISSUER, "aud": AUDIENCE, "iat": int(time.time())},
            None,
            algorithm="none",
        ),  # unsigned
        "not-a-token",
    ],
)
def test_bad_tokens_are_rejected(token):
    with pytest.raises(AuthError):
        TokenIssuer(SECRET).verify(token)


def test_missing_claim_rejected():
    tok = jwt.encode(
        {"sub": "u", "exp": int(time.time()) + 60, "iss": ISSUER, "aud": AUDIENCE}, SECRET, algorithm=ALGORITHM
    )
    with pytest.raises(AuthError, match="iat"):
        TokenIssuer(SECRET).verify(tok)


def test_short_secret_refused():
    with pytest.raises(ValueError):
        TokenIssuer("short")
