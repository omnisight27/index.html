"""Coinbase CDP (Advanced Trade) read-only client.

Auth: every request carries a short-lived ES256 JWT signed with the user's
CDP API private key, per Coinbase's spec:
  header:  {alg: ES256, kid: <key name>, nonce: <random hex>, typ: JWT}
  claims:  iss "cdp", sub <key name>, aud ["retail_rest_api"],
           uri "<METHOD> api.coinbase.com<path>", nbf/exp (120s window)
Only READ endpoints are ever called: GET /api/v3/brokerage/accounts and
GET /api/v3/brokerage/orders/historical/fills (fills feed the cost-basis
math only — units still come from accounts).
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
FILLS_PATH = "/api/v3/brokerage/orders/historical/fills"
FILLS_PAGE_LIMIT = 250
FILLS_MAX_TOTAL = 2500


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


def _parse_float(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _fill_time(f: dict) -> float:
    """Sortable trade time; unparseable/missing times sort last (sells with
    unknown time then reduce the final basis rather than zeroing early)."""
    from datetime import datetime, timezone

    if not isinstance(f, dict):
        return float("inf")
    ts = f.get("trade_time")
    if not ts:
        return float("inf")
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return float("inf")


def average_costs(fills: list[dict]) -> dict[str, float | None]:
    """Weighted-average cost per base currency from fill history.

    BUY:  cost += size*price + commission; qty += size
    SELL: if tracked qty > 0: cost *= max(0, (qty-size)/qty); qty -= size
    Fills process oldest-first. Skips fills with missing/zero size or price,
    unknown side, or no product id. avg = cost/qty if qty > 0 else None.
    """
    lots: dict[str, dict] = {}
    for f in sorted(fills or [], key=_fill_time):
        if not isinstance(f, dict):
            continue
        side = str(f.get("side") or "").upper()
        if side not in ("BUY", "SELL"):
            continue
        product = str(f.get("product_id") or "")
        if "-" not in product:
            continue
        base = product.split("-", 1)[0].upper()
        if not base:
            continue
        size = _parse_float(f.get("size"))
        price = _parse_float(f.get("price"))
        if size is None or price is None or size <= 0 or price <= 0:
            continue
        lot = lots.setdefault(base, {"qty": 0.0, "cost": 0.0})
        if side == "BUY":
            commission = _parse_float(f.get("commission")) or 0.0
            lot["qty"] += size
            lot["cost"] += size * price + commission
        else:  # SELL
            if lot["qty"] > 0:
                lot["cost"] *= max(0.0, (lot["qty"] - size) / lot["qty"])
                lot["qty"] -= size
    out: dict[str, float | None] = {}
    for base, lot in lots.items():
        out[base] = lot["cost"] / lot["qty"] if lot["qty"] > 0 else None
    return out


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

    # -- trade fills (read-only) --------------------------------------------
    def fetch_fills(self) -> list[dict]:
        """All historical fills, oldest-first not guaranteed — callers sort.

        Paginated: limit=250 per page, follow the response cursor. Caps at
        ~2,500 fills; stops early when a page returns fewer than the limit.
        Fail-open: ANY error returns [] so the portfolio endpoint keeps
        working with balances only.
        """
        fills: list[dict] = []
        try:
            cursor = None
            while True:
                params = {"limit": FILLS_PAGE_LIMIT}
                if cursor:
                    params["cursor"] = cursor
                token = build_jwt(self.key_name, self.private_key_pem, "GET", FILLS_PATH)
                r = self.client.get(
                    FILLS_PATH,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
                if r.status_code != 200:
                    return []
                body = r.json()
                page = body.get("fills") or []
                fills.extend(page)
                cursor = body.get("cursor")
                if len(fills) >= FILLS_MAX_TOTAL:
                    return fills[:FILLS_MAX_TOTAL]
                if not cursor or len(page) < FILLS_PAGE_LIMIT:
                    return fills
        except Exception:
            return []


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
        staked, staked_units, avg_cost_usd}]. Units include amounts on hold
        (open orders) — the user owns them either way. Zero-balance accounts
        are dropped. avg_cost_usd comes from the user's own fill history
        (weighted average, buys minus proportional sells); None when the
        fills can't establish a basis (fail-open: balances always work)."""
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
            # Drop dust: zero-unit positions, or positions worth less than $1.
            # (Zero-unit rows with no price slip past the value check, so test units too.)
            if row["units"] == 0:
                continue
            if row["value_usd"] is not None and row["value_usd"] < 1.0:
                continue
            row["units"] = round(row["units"], 10)
            row["staked_units"] = round(row["staked_units"], 10)
            out.append(row)
        # Cost basis from fill history — fail-open: fills failing never
        # breaks the balances above.
        try:
            costs = average_costs(self.fetch_fills())
        except Exception:
            costs = {}
        for row in out:
            avg = costs.get(row["symbol"])
            row["avg_cost_usd"] = round(avg, 2) if avg is not None else None
        return out
