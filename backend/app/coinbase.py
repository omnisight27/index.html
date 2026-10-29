"""Coinbase CDP (Advanced Trade) read-only client.

Auth: every request carries a short-lived ES256 JWT signed with the user's
CDP API private key, per Coinbase's spec:
  header:  {alg: ES256, kid: <key name>, nonce: <random hex>, typ: JWT}
  claims:  iss "cdp", sub <key name>, aud ["retail_rest_api"],
           uri "<METHOD> api.coinbase.com<path>", nbf/exp (120s window)
Only READ endpoints are ever called: GET /api/v3/brokerage/accounts.
The user's key should be View-only (the UI instructs this); this client has
no code path that could trade or transfer regardless.

Known API limitation — staking: the brokerage accounts endpoint does NOT
expose a clean "staked balance" field. Staked assets show up as their own
account whose name mentions staking (e.g. "Ethereum Staking") — we detect
that (account name or type containing "stak") and roll those units into the
currency's position flagged as staked. If Coinbase changes that shape,
positions still come through, just with staked=false.
"""
import secrets
import time

import httpx
import jwt as pyjwt
from cryptography.hazmat.primitives.serialization import load_pem_private_key

API_HOST = "api.coinbase.com"
API_BASE = f"https://{API_HOST}"
EXCHANGE_BASE = "https://api.exchange.coinbase.com"
ACCOUNTS_PATH = "/api/v3/brokerage/accounts"


class CoinbaseError(RuntimeError):
    pass


def load_ec_private_key(pem: str):
    """Parse a CDP private key PEM; must be an EC key. Raises ValueError."""
    # Tolerate pastes copied from a code sample where newlines are escaped
    # (literal backslash-n). A valid PEM never contains a backslash, so this
    # replacement is unambiguous and a no-op for correctly pasted keys.
    if "\\n" in pem:
        pem = pem.replace("\\n", "\n")
    try:
        key = load_pem_private_key(pem.encode(), password=None)
    except Exception as e:
        raise ValueError(f"private key is not a valid PEM: {e}")
    from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey

    if not isinstance(key, EllipticCurvePrivateKey):
        raise ValueError("private key is not an EC key (CDP keys are EC keys)")
    return key


def build_jwt(key_name: str, private_key_pem: str, method: str, path: str) -> str:
    key = load_ec_private_key(private_key_pem)
    now = int(time.time())
    claims = {
        "sub": key_name,
        "iss": "cdp",
        "aud": ["retail_rest_api"],
        "nbf": now,
        "exp": now + 120,
        "uri": f"{method} {API_HOST}{path}",
    }
    headers = {"kid": key_name, "nonce": secrets.token_hex(16), "typ": "JWT"}
    return pyjwt.encode(claims, key, algorithm="ES256", headers=headers)


class CoinbaseClient:
    """`transport` injectable (httpx.MockTransport in tests)."""

    def __init__(self, key_name: str, private_key_pem: str, transport=None):
        self.key_name = key_name
        self.private_key_pem = private_key_pem
        self.client = httpx.Client(
            base_url=API_BASE, transport=transport, timeout=20.0
        )
        self.public_client = httpx.Client(
            base_url=EXCHANGE_BASE, transport=transport, timeout=15.0
        )
        self._price_cache: dict[str, float | None] = {}

    # -- raw accounts -------------------------------------------------------
    def fetch_accounts(self) -> list[dict]:
        accounts: list[dict] = []
        cursor = None
        while True:
            params = {"limit": 250}
            if cursor:
                params["cursor"] = cursor
            token = build_jwt(self.key_name, self.private_key_pem, "GET", ACCOUNTS_PATH)
            r = self.client.get(
                ACCOUNTS_PATH,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code != 200:
                raise CoinbaseError(
                    f"Coinbase accounts request failed ({r.status_code}): {r.text[:200]}"
                )
            body = r.json()
            accounts.extend(body.get("accounts", []))
            if body.get("has_next") and body.get("cursor"):
                cursor = body["cursor"]
            else:
                return accounts

    # -- USD pricing (public, keyless spot tickers) --------------------------
    def price_usd(self, symbol: str) -> float | None:
        if symbol in self._price_cache:
            return self._price_cache[symbol]
        price = None
        if symbol in ("USD",):
            price = 1.0
        else:
            try:
                r = self.public_client.get(f"/products/{symbol}-USD/ticker")
                if r.status_code == 200:
                    price = float(r.json()["price"])
            except Exception:
                price = None
        self._price_cache[symbol] = price
        return price

    # -- normalization -------------------------------------------------------
    def portfolio(self) -> list[dict]:
        """Normalize accounts to [{symbol, units, price_usd, value_usd,
        staked, staked_units}]. Units include amounts on hold (open orders) —
        the user owns them either way. Zero-balance accounts are dropped."""
        merged: dict[str, dict] = {}
        for acct in self.fetch_accounts():
            symbol = (acct.get("currency") or "").upper()
            if not symbol:
                continue
            avail = float((acct.get("available_balance") or {}).get("value") or 0)
            hold = float((acct.get("hold") or {}).get("value") or 0)
            units = avail + hold
            if units <= 0:
                continue
            name = (acct.get("name") or "").lower()
            atype = (acct.get("type") or "").lower()
            is_staked = "stak" in name or "stak" in atype
            row = merged.setdefault(
                symbol,
                {"symbol": symbol, "units": 0.0, "staked": False, "staked_units": 0.0},
            )
            row["units"] += units
            if is_staked:
                row["staked"] = True
                row["staked_units"] += units
        out = []
        for symbol in sorted(merged):
            row = merged[symbol]
            price = self.price_usd(symbol)
            row["price_usd"] = price
            row["value_usd"] = round(row["units"] * price, 2) if price else None
            row["units"] = round(row["units"], 10)
            row["staked_units"] = round(row["staked_units"], 10)
            out.append(row)
        return out
