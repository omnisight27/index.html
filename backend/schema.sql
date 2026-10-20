-- OmniSight — Supabase schema for account linking (Phase 1).
-- Run this once in the Supabase dashboard: SQL Editor → New query → paste → Run.
--
-- One row per (user, provider): the user's exchange credentials, stored ONLY
-- as an encrypted blob (credentials_enc = Fernet ciphertext produced by the
-- backend; the plaintext key never touches this database or the frontend).

create table if not exists public.user_credentials (
    user_id         uuid not null references auth.users (id) on delete cascade,
    provider        text not null,                       -- 'coinbase'
    key_name_masked text not null,                       -- e.g. '…a1b2c3' (display only)
    credentials_enc text not null,                       -- Fernet-encrypted JSON {key_name, private_key}
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    primary key (user_id, provider)
);

-- Row Level Security: ON, with NO policies for anon/authenticated roles.
-- That means the public anon key can read/write NOTHING in this table.
-- The backend reaches it with the service-role key (server-side only),
-- which bypasses RLS by design and always filters by user_id itself.
alter table public.user_credentials enable row level security;
