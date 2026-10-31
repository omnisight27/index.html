"""Tests for fills -> weighted-average cost basis and fetch_fills pagination.

Coinbase transport is fully mocked (httpx.MockTransport); no network, no
real keys. Run: python -m pytest backend/tests -v
"""
import os
import time

os.environ.setdefault("SUPABASE_JWT_SECRET", "test-jwt-secret-that-is-long-enough-32b")

# A real Fernet key for the test run (generated fresh, never used in prod).
from cryptography.fernet import Fernet  # noqa: E402

os.environ["CRED_ENC_KEY"] = Fernet.generate_key().decode()

import sys  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx  # noqa: E402
import jwt as pyjwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.serialization import (  # noqa: E402
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from fastapi.testclient import TestClient  # noqa: E402

from app.coinbase import CoinbaseClient, average_costs  # noqa: E402
from app.main import create_app  # noqa: E402
from app.store import InMemoryStore  # noqa: E402

USER_ID = "11111111-2222-3333-4444-555555555555"
KEY_NAME = "organizations/test-org/apiKeys/testkey123456"
DUMMY_PEM = None  # built lazily; real EC key needed only to sign test JWTs


def make_ec_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption())
    return pem.decode()


def get_pem() -> str:
    global DUMMY_PEM
    if DUMMY_PEM is None:
        DUMMY_PEM = make_ec_pem()
    return DUMMY_PEM


def user_headers():
    now = int(time.time())
    token = pyjwt.encode(
        {"sub": USER_ID, "aud": "authenticated", "iat": now, "exp": now + 3600},
        "test-jwt-secret-that-is-long-enough-32b",
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def fill(side, product, size, price, commission="0.00", trade_time="2026-01-15T12:00:00Z"):
    return {
        "side": side,
        "product_id": product,
        "size": size,
        "price": price,
        "commission": commission,
        "trade_time": trade_time,
    }


# -- average_costs: buys ----------------------------------------------------

def test_buys_only_weighted_average():
    fills = [
        fill("BUY", "BTC-USD", "1.0", "100.00", trade_time="2026-01-01T00:00:00Z"),
        fill("BUY", "BTC-USD", "1.0", "200.00", trade_time="2026-02-01T00:00:00Z"),
    ]
    assert average_costs(fills) == {"BTC": 150.0}


def test_commission_included_in_cost():
    fills = [fill("BUY", "BTC-USD", "1.0", "100.00", commission="5.00")]
    assert average_costs(fills) == {"BTC": 105.0}


def test_chronological_ordering_newest_first_input():
    # Fills arrive newest-first; math must still process oldest-first.
    fills = [
        fill("SELL", "BTC-USD", "1.0", "250.00", trade_time="2026-03-01T00:00:00Z"),
        fill("BUY", "BTC-USD", "1.0", "200.00", trade_time="2026-02-01T00:00:00Z"),
        fill("BUY", "BTC-USD", "1.0", "100.00", trade_time="2026-01-01T00:00:00Z"),
    ]
    # cost 300 over 2 units, sell half the position -> avg stays 150
    assert average_costs(fills) == {"BTC": 150.0}


# -- average_costs: sells ---------------------------------------------------

def test_buy_then_partial_sell_proportional():
    fills = [
        fill("BUY", "BTC-USD", "2.0", "100.00", trade_time="2026-01-01T00:00:00Z"),
        fill("SELL", "BTC-USD", "0.5", "150.00", trade_time="2026-02-01T00:00:00Z"),
    ]
    # cost 200 * (1.5/2) = 150 over 1.5 units -> avg 100
    assert average_costs(fills) == {"BTC": 100.0}


def test_sell_everything_floors_to_none():
    fills = [
        fill("BUY", "BTC-USD", "2.0", "100.00", trade_time="2026-01-01T00:00:00Z"),
        fill("SELL", "BTC-USD", "2.0", "150.00", trade_time="2026-02-01T00:00:00Z"),
    ]
    assert average_costs(fills) == {"BTC": None}


def test_sell_exceeding_tracked_qty_floors_at_zero():
    fills = [
        fill("BUY", "BTC-USD", "1.0", "100.00", trade_time="2026-01-01T00:00:00Z"),
        fill("SELL", "BTC-USD", "3.0", "150.00", trade_time="2026-02-01T00:00:00Z"),
    ]
    assert average_costs(fills) == {"BTC": None}


def test_sell_with_no_buys_gives_none():
    fills = [fill("SELL", "BTC-USD", "1.0", "150.00")]
    assert average_costs(fills) == {"BTC": None}


# -- average_costs: edge cases ----------------------------------------------

def test_empty_fills():
    assert average_costs([]) == {}
    assert average_costs(None) == {}


def test_malformed_fills_skipped():
    good = fill("BUY", "BTC-USD", "1.0", "100.00")
    bad = [
        fill("BUY", "BTC-USD", "abc", "100.00"),      # non-numeric size
        fill("BUY", "BTC-USD", "1.0", None),          # missing price
        fill("BUY", "BTC-USD", "0", "100.00"),        # zero size
        fill("BUY", "BTC-USD", "1.0", "0"),           # zero price
        fill("HOLD", "BTC-USD", "1.0", "100.00"),     # unknown side
        fill("BUY", "BTCUSD", "1.0", "100.00"),       # no dash in product
        fill("BUY", None, "1.0", "100.00"),           # missing product
        None,                                         # not a dict
        "nonsense",                                   # not a dict
    ]
    assert average_costs([good] + bad) == {"BTC": 100.0}
    # Bad commission parses defensively to 0, doesn't kill the fill.
    assert average_costs([fill("BUY", "BTC-USD", "1.0", "100.00", commission="xyz")]) == {"BTC": 100.0}


def test_multi_currency_separation():
    fills = [
        fill("BUY", "BTC-USD", "1.0", "100.00"),
        fill("BUY", "ETH-USD", "2.0", "50.00"),
        fill("SELL", "ETH-USD", "1.0", "60.00"),
    ]
    assert average_costs(fills) == {"BTC": 100.0, "ETH": 50.0}


# -- fetch_fills pagination -------------------------------------------------

def _fills_transport(pages):
    """pages: list of (fills_list, cursor_or_None) tuples, served in order."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        idx = len(seen) - 1
        fills_page, cursor = pages[min(idx, len(pages) - 1)]
        body = {"fills": fills_page}
        if cursor:
            body["cursor"] = cursor
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler), seen


def test_fetch_fills_follows_cursor_and_stops_on_short_page():
    page1 = [fill("BUY", "BTC-USD", "0.01", "80000.00") for _ in range(250)]
    page2 = [fill("BUY", "BTC-USD", "0.01", "81000.00") for _ in range(100)]
    transport, seen = _fills_transport([(page1, "cursor-2"), (page2, None)])
    client = CoinbaseClient(KEY_NAME, get_pem(), transport=transport)
    fills = client.fetch_fills()
    assert len(fills) == 350
    assert len(seen) == 2
    # Second request carried the cursor from the first response.
    assert seen[1].url.params.get("cursor") == "cursor-2"
    assert seen[0].url.params.get("limit") == "250"


def test_fetch_fills_fail_open_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    client = CoinbaseClient(KEY_NAME, get_pem(), transport=httpx.MockTransport(handler))
    assert client.fetch_fills() == []


def test_fetch_fills_caps_at_2500():
    full_page = [fill("BUY", "BTC-USD", "0.01", "80000.00") for _ in range(250)]

    def handler(request: httpx.Request) -> httpx.Response:
        # Endless full pages with cursors — client must stop itself.
        return httpx.Response(200, json={"fills": full_page, "cursor": "more"})

    transport = httpx.MockTransport(handler)
    client = CoinbaseClient(KEY_NAME, get_pem(), transport=transport)
    fills = client.fetch_fills()
    assert len(fills) == 2500


# -- endpoint wiring: avg_cost_usd on the portfolio response ----------------

def _wired_transport(fills):
    accounts = {
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
        ],
        "has_next": False, "cursor": "",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "api.coinbase.com" and url.path == "/api/v3/brokerage/accounts":
            return httpx.Response(200, json=accounts)
        if url.host == "api.coinbase.com" and url.path == "/api/v3/brokerage/orders/historical/fills":
            return httpx.Response(200, json={"fills": fills})
        if url.host == "api.exchange.coinbase.com" and url.path.startswith("/products/"):
            return httpx.Response(200, json={"price": "80000.00"})
        return httpx.Response(404, json={"message": "unmocked"})

    return httpx.MockTransport(handler)


def test_portfolio_carries_avg_cost_usd():
    fills = [fill("BUY", "BTC-USD", "0.5", "80000.00", commission="10.00")]
    transport = _wired_transport(fills)
    app = create_app(
        store=InMemoryStore(),
        coinbase_client_factory=lambda kn, pk: CoinbaseClient(kn, pk, transport=transport),
    )
    tc = TestClient(app)
    r = tc.post(
        "/link/coinbase",
        json={"key_name": KEY_NAME, "private_key": get_pem()},
        headers=user_headers(),
    )
    assert r.status_code == 200, r.text

    r = tc.get("/portfolio/coinbase", headers=user_headers())
    assert r.status_code == 200, r.text
    positions = {p["symbol"]: p for p in r.json()}
    # (0.5*80000 + 10) / 0.5 = 80020.00
    assert positions["BTC"]["avg_cost_usd"] == 80020.0
    assert positions["BTC"]["units"] == 0.5  # existing fields untouched
    # ETH has no fills -> None, not a crash.
    assert positions["ETH"]["avg_cost_usd"] is None


def test_portfolio_fail_open_when_fills_error():
    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.path == "/api/v3/brokerage/orders/historical/fills":
            return httpx.Response(500, json={"message": "boom"})
        return _wired_transport([]).handle_request(request)

    app = create_app(
        store=InMemoryStore(),
        coinbase_client_factory=lambda kn, pk: CoinbaseClient(kn, pk, transport=httpx.MockTransport(handler)),
    )
    tc = TestClient(app)
    r = tc.post(
        "/link/coinbase",
        json={"key_name": KEY_NAME, "private_key": get_pem()},
        headers=user_headers(),
    )
    assert r.status_code == 200, r.text
    r = tc.get("/portfolio/coinbase", headers=user_headers())
    assert r.status_code == 200, r.text
    positions = {p["symbol"]: p for p in r.json()}
    assert positions["BTC"]["avg_cost_usd"] is None
    assert positions["BTC"]["units"] == 0.5
