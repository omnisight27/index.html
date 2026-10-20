"""Encryption at rest + Supabase JWT verification."""
import json
import os

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


def verify_supabase_jwt(token: str) -> str:
    """Verify a Supabase access token, return the user id (`sub` claim).

    Supabase signs user JWTs. Projects expose a JWT secret (Project Settings →
    API → JWT Secret) used for HS256 verification of the legacy/shared secret.
    If the project has been rotated to asymmetric signing keys, swap this for a
    JWKS check against {SUPABASE_URL}/auth/v1/.well-known/jwks.json — the rest
    of the service only depends on the returned user id.
    """
    secret = os.environ.get("SUPABASE_JWT_SECRET", "")
    if not secret:
        raise ConfigError("SUPABASE_JWT_SECRET is not set")
    payload = jwt.decode(
        token,
        secret,
        algorithms=["HS256"],
        audience="authenticated",
        options={"require": ["exp", "sub", "aud"]},
    )
    user_id = payload.get("sub")
    if not user_id:
        raise jwt.InvalidTokenError("token has no sub claim")
    return user_id
