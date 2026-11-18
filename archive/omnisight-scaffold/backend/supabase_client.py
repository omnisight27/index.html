from backend.config import settings
from supabase import create_client

if not settings.SUPABASE_URL:
    raise ValueError("SUPABASE_URL is not configured")
if not settings.SUPABASE_KEY:
    raise ValueError("SUPABASE_KEY is not configured")

supabase = create_client(
    settings.SUPABASE_URL,
    settings.SUPABASE_KEY,
)
