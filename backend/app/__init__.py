"""OmniSight backend — credential custody + Coinbase read-only portfolio sync.

Security model:
- Users authenticate with a Supabase JWT (verified here against the project's
  JWT secret). The user's id (`sub` claim) keys every credential row.
- Coinbase CDP credentials are stored ONLY as a Fernet-encrypted blob in
  Supabase Postgres (see schema.sql). Plaintext keys exist in memory only for
  the duration of one Coinbase request.
- The frontend never sees a Coinbase key. Keys are never logged.
"""
