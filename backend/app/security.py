"""Encryption at rest + Supabase JWT verification."""
import json
import os
import time
import urllib.request

import jwt
from cryptography.fernet import Fernet, InvalidToken


class ConfigError(RuntimeError):
    pass


def get_fernet() -> Fernet:
    """Fernet instance from CRED_ENC_KEY env var (urlsafe-base64 32-byte key).

    Generate one with:
        python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    The key lives ONLY in the hosting environment (Render dashboard) — never in
    git, never in the frontend. Losing it means stored credentials cannot be
    decrypted and users must re-link; rotating it requires re-linking too.
    """
    key = os.environ.get("CRED_ENC_KEY", "")
    if not key:
        raise ConfigError("CRED_ENC_KEY is not set")
    try:
        return Fernet(key.encode())
    except Exception as e:  # malformed key
        raise ConfigError(f"CRED_ENC_KEY is not a valid Fernet key: {e}")


def encrypt_credentials(key_name: str, private_key_pem: str) -> str:
    blob = json.dumps({"key_name": key_name, "private_key": private_key_pem})
    return get_fernet().encrypt(blob.encode()).decode()


def decrypt_credentials(ciphertext: str) -> dict:
    try:
        blob = get_fernet().decrypt(ciphertext.encode())
    except InvalidToken:
        raise ConfigError("Stored credentials cannot be decrypted (CRED_ENC_KEY changed?)")
    return json.loads(blob)


_JWKS_CACHE = {"keys": None, "fetched_at": 0.0}


def _jwks_public_key(kid: str):
    """Public key for a Supabase ES256 signing key, from the project's JWKS.

    New Supabase projects sign user tokens with ES256 asymmetric keys, not
    the legacy HS256 shared secret, so HS256-only verification rejects them
    with "The specified alg value is not allowed". JWKS is public by design.
    Cached 10 minutes so a key rotation is picked up promptly.
    """
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not supabase_url:
        raise ConfigError("SUPABASE_URL is not set")
    now = time.time()
    if _JWKS_CACHE["keys"] is None or now - _JWKS_CACHE["fetched_at"] > 600:
        jwks_url = supabase_url + "/auth/v1/.well-known/jwks.json"
        try:
            with urllib.request.urlopen(jwks_url, timeout=10) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:
            raise jwt.InvalidTokenError(f"could not fetch Supabase JWKS: {e}")
        _JWKS_CACHE["keys"] = data.get("keys", [])
        _JWKS_CACHE["fetched_at"] = now
    for key in _JWKS_CACHE["keys"]:
        if key.get("kid") == kid and key.get("kty") == "EC":
            return jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(key))
    raise jwt.InvalidTokenError("no matching Supabase signing key in JWKS")


def verify_supabase_jwt(token: str) -> str:
    """Verify a Supabase access token, return the user id (`sub` claim).

    Accepts both signing styles Supabase actually issues:
    - HS256 with the legacy/shared JWT secret (older projects), and
    - ES256 with the project's JWKS keys (new projects, default since 2025).
    Anything else is rejected, never guessed.
    """
    try:
        header = jwt.get_unverified_header(token)
    except Exception as e:
        raise jwt.InvalidTokenError(str(e))
    alg = header.get("alg")
    if alg == "HS256":
        secret = os.environ.get("SUPABASE_JWT_SECRET", "")
        if not secret:
            raise ConfigError("SUPABASE_JWT_SECRET is not set")
        key = secret
        algorithms = ["HS256"]
    elif alg == "ES256":
        key = _jwks_public_key(header.get("kid"))
        algorithms = ["ES256"]
    else:
        raise jwt.InvalidTokenError("The specified alg value is not allowed")
    payload = jwt.decode(
        token,
        key,
        algorithms=algorithms,
        audience="authenticated",
        options={"require": ["exp", "sub", "aud"]},
    )
    user_id = payload.get("sub")
    if not user_id:
        raise jwt.InvalidTokenError("token has no sub claim")
    return user_id
