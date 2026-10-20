"""OmniSight backend API.

Endpoints (all except /health require a Supabase JWT bearer token):
  GET    /health
  POST   /link/coinbase        store a user's Coinbase CDP key (encrypted)
  GET    /link/status          is a Coinbase key linked? (masked name only)
  GET    /portfolio/coinbase   normalized balances from Coinbase, read-only
  DELETE /link/coinbase        remove stored credentials

Run locally:  uvicorn app.main:app --reload --port 8000
"""
import os

import jwt as pyjwt
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import security
from .coinbase import CoinbaseClient, CoinbaseError, load_ec_private_key
from .store import InMemoryStore, SupabaseStore

PROVIDER = "coinbase"

DEFAULT_ORIGINS = (
    "https://omnisight27.github.io,"
    "http://localhost:8000,http://127.0.0.1:8000,"
    "http://localhost:5500,http://127.0.0.1:5500"
)


def mask_key_name(key_name: str) -> str:
    """Show only the tail of the key name: '…a1b2c3'."""
    tail = key_name[-6:] if len(key_name) > 6 else key_name
    return f"…{tail}"


class LinkCoinbaseIn(BaseModel):
    key_name: str = Field(min_length=3, max_length=300)
    private_key: str = Field(min_length=20, max_length=10000)


def create_app(store=None, coinbase_client_factory=None) -> FastAPI:
    app = FastAPI(title="OmniSight Backend", version="1.0.0")

    origins = os.environ.get("CORS_ORIGINS", DEFAULT_ORIGINS).split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in origins if o.strip()],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    def get_store():
        if store is not None:
            return store
        return SupabaseStore()

    def make_coinbase(key_name, private_key):
        if coinbase_client_factory is not None:
            return coinbase_client_factory(key_name, private_key)
        return CoinbaseClient(key_name, private_key)

    def current_user(authorization: str = Header(default="")) -> str:
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Missing bearer token")
        token = authorization.split(" ", 1)[1].strip()
        try:
            return security.verify_supabase_jwt(token)
        except security.ConfigError as e:
            raise HTTPException(status_code=500, detail=str(e))
        except pyjwt.InvalidTokenError as e:
            raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/link/coinbase")
    def link_coinbase(body: LinkCoinbaseIn, user_id: str = Depends(current_user)):
        key_name = body.key_name.strip()
        private_key = body.private_key.strip()
        try:
            load_ec_private_key(private_key)  # validate before storing anything
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        try:
            enc = security.encrypt_credentials(key_name, private_key)
        except security.ConfigError as e:
            raise HTTPException(status_code=500, detail=str(e))
        row = get_store().upsert(user_id, PROVIDER, mask_key_name(key_name), enc)
        return {
            "linked": True,
            "key_name_masked": row["key_name_masked"],
            "updated_at": row.get("updated_at"),
        }

    @app.get("/link/status")
    def link_status(user_id: str = Depends(current_user)):
        row = get_store().get(user_id, PROVIDER)
        if not row:
            return {"linked": False, "key_name_masked": None, "updated_at": None}
        return {
            "linked": True,
            "key_name_masked": row.get("key_name_masked"),
            "updated_at": row.get("updated_at"),
        }

    @app.get("/portfolio/coinbase")
    def coinbase_portfolio(user_id: str = Depends(current_user)):
        row = get_store().get(user_id, PROVIDER)
        if not row:
            raise HTTPException(status_code=400, detail="Coinbase account not linked")
        try:
            creds = security.decrypt_credentials(row["credentials_enc"])
        except security.ConfigError as e:
            raise HTTPException(status_code=500, detail=str(e))
        client = make_coinbase(creds["key_name"], creds["private_key"])
        try:
            return client.portfolio()
        except CoinbaseError as e:
            raise HTTPException(status_code=502, detail=str(e))

    @app.delete("/link/coinbase")
    def unlink_coinbase(user_id: str = Depends(current_user)):
        get_store().delete(user_id, PROVIDER)
        return {"linked": False}

    return app


app = create_app()
