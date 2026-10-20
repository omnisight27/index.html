"""Backend tests — Coinbase transport is fully mocked (httpx.MockTransport);
no network, no real keys. Run: python -m pytest backend/tests -v"""
import os
import time

os.environ.setdefault("SUPABASE_JWT_SECRET", "test-jwt-secret-that-is-long-enough-32b")

# A real Fernet key for the test run (generated fresh, never used in prod).
from cryptography.fernet import Fernet  # noqa: E402

os.environ["CRED_ENC_KEY"] = Fernet.generate_key().decode()

import httpx  # noqa: E402
import jwt as pyjwt  # noqa: E402
import pytest  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.serialization import (  # noqa: E402
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from fastapi.testclient import TestClient  # noqa: E402

import sys  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.coinbase import CoinbaseClient  # noqa: E402
from app.main import create_app  # noqa: E402
from app.store import InMemoryStore  # noqa: E402

USER_ID = "11111111-2222-3333-4444-555555555555"
KEY_NAME = "organizations/test-org/apiKeys/testkey123456"


def make_ec_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption())
    return pem.decode()


def user_headers():
    now = int(time.time())
    token = pyjwt.encode(
        {"sub": USER_ID, "aud": "authenticated", "iat": now, "exp": now + 3600},
        "test-jwt-secret-that-is-long-enough-32b",
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


ACCOUNTS_PAGE = {
    "accounts": [
        {
            "uuid": "a1", "name": "BTC Wallet", "currency": "BTC",
            "type": "ACCOUNT_TYPE_CRYPTO",
            "available_balance": {"value": "0.5", "currency": "BTC"},
            "hold": {"value": "0.0", "currency": "BTC"},
        },
        {
            "uuid": "a2", "name": "ETH Wallet", "currency": "ETH",
            "type": "ACCOUNT_TYPE_CRYPTO",
            "available_balance": {"value": "1.0", "currency": "ETH"},
            "hold": {"value": "0.0", "currency": "ETH"},
        },
        {
            # Staked-ETH style account: separate account, staking in the name.
            "uuid": "a3", "name": "Ethereum Staking", "currency": "ETH",
            "type": "ACCOUNT_TYPE_CRYPTO",
            "available_balance": {"value": "2.0", "currency": "ETH"},
            "hold": {"value": "0.0", "currency": "ETH"},
        },
        {
            "uuid": "a4", "name": "AVAX Wallet", "currency": "AVAX",
            "type": "ACCOUNT_TYPE_CRYPTO",
            "available_balance": {"value": "0.0", "currency": "AVAX"},
            "hold": {"value": "0.0", "currency": "AVAX"},
        },
        {
            "uuid": "a5", "name": "USD Wallet", "currency": "USD",
            "type": "ACCOUNT_TYPE_FIAT",
            "available_balance": {"value": "150.25", "currency": "USD"},
            "hold": {"value": "0.0", "currency": "USD"},
        },
    ],
    "has_next": False, "cursor": "", "size": 5,
}

PRICES = {"BTC": "80000.00", "ETH": "2500.00"}


def mock_coinbase_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "api.coinbase.com" and url.path == "/api/v3/brokerage/accounts":
            assert request.headers.get("authorization", "").startswith("Bearer ")
            return httpx.Response(200, json=ACCOUNTS_PAGE)
        if url.host == "api.exchange.coinbase.com" and url.path.startswith("/products/"):
            symbol = url.path.split("/")[2].split("-")[0]
            if symbol in PRICES:
                return httpx.Response(200, json={"price": PRICES[symbol]})
            return httpx.Response(404, json={"message": "not found"})
        return httpx.Response(404, json={"message": f"unmocked {url}"})

    return httpx.MockTransport(handler)


@pytest.fixture()
def client():
    store = InMemoryStore()
    transport = mock_coinbase_transport()
    app = create_app(
        store=store,
        coinbase_client_factory=lambda kn, pk: CoinbaseClient(kn, pk, transport=transport),
    )
    return TestClient(app), store


def link_ok(client):
    tc, _ = client
    r = tc.post(
        "/link/coinbase",
        json={"key_name": KEY_NAME, "private_key": make_ec_pem()},
        headers=user_headers(),
    )
    assert r.status_code == 200, r.text
    return r


def test_health(client):
    tc, _ = client
    assert tc.get("/health").json() == {"status": "ok"}


def test_auth_required(client):
    tc, _ = client
    assert tc.get("/link/status").status_code == 401
    assert tc.get("/portfolio/coinbase").status_code == 401
    assert tc.post("/link/coinbase", json={"key_name": "x" * 5, "private_key": "y" * 30}).status_code == 401


def test_link_rejects_bad_key(client):
    tc, _ = client
    r = tc.post(
        "/link/coinbase",
        json={"key_name": KEY_NAME, "private_key": "-----BEGIN EC PRIVATE KEY-----\nnot-base64!!\n-----END EC PRIVATE KEY-----"},
        headers=user_headers(),
    )
    assert r.status_code == 400


def test_link_stores_encrypted_and_status_masked(client):
    tc, store = client
    pem = make_ec_pem()
    r = tc.post(
        "/link/coinbase",
        json={"key_name": KEY_NAME, "private_key": pem},
        headers=user_headers(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["linked"] is True
    assert body["key_name_masked"] == "…123456"
    assert KEY_NAME not in str(body)

    row = store.get(USER_ID, "coinbase")
    assert row is not None
    # The stored blob must be ciphertext: no PEM header, no key name inside.
    assert "PRIVATE KEY" not in row["credentials_enc"]
    assert "testkey123456" not in row["credentials_enc"]

    r = tc.get("/link/status", headers=user_headers())
    assert r.json()["linked"] is True
    assert r.json()["key_name_masked"] == "…123456"


def test_portfolio_requires_link(client):
    tc, _ = client
    r = tc.get("/portfolio/coinbase", headers=user_headers())
    assert r.status_code == 400


def test_portfolio_normalization(client):
    tc, _ = client
    link_ok(client)
    r = tc.get("/portfolio/coinbase", headers=user_headers())
    assert r.status_code == 200, r.text
    positions = {p["symbol"]: p for p in r.json()}

    # Zero-balance AVAX dropped; ETH wallet + staking merged.
    assert set(positions) == {"BTC", "ETH", "USD"}

    btc = positions["BTC"]
    assert btc["units"] == 0.5 and btc["value_usd"] == 40000.0 and btc["staked"] is False

    eth = positions["ETH"]
    assert eth["units"] == 3.0
    assert eth["staked"] is True and eth["staked_units"] == 2.0
    assert eth["value_usd"] == 7500.0

    usd = positions["USD"]
    assert usd["units"] == 150.25 and usd["value_usd"] == 150.25


def test_portfolio_empty_account(client):
    store = InMemoryStore()

    def empty_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.coinbase.com":
            return httpx.Response(200, json={"accounts": [], "has_next": False, "cursor": "", "size": 0})
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(empty_handler)
    app = create_app(
        store=store,
        coinbase_client_factory=lambda kn, pk: CoinbaseClient(kn, pk, transport=transport),
    )
    tc = TestClient(app)
    r = tc.post(
        "/link/coinbase",
        json={"key_name": KEY_NAME, "private_key": make_ec_pem()},
        headers=user_headers(),
    )
    assert r.status_code == 200
    r = tc.get("/portfolio/coinbase", headers=user_headers())
    assert r.status_code == 200 and r.json() == []


def test_unlink(client):
    tc, _ = client
    link_ok(client)
    r = tc.delete("/link/coinbase", headers=user_headers())
    assert r.json() == {"linked": False}
    assert tc.get("/link/status", headers=user_headers()).json()["linked"] is False
    assert tc.get("/portfolio/coinbase", headers=user_headers()).status_code == 400
