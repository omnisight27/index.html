from backend.supabase_client import supabase


class SupabaseMemoryService:
    def get_memories(self):
        response = supabase.table("memories").select("*").execute()
        return response.data

    def create_memory(self, data):
        response = supabase.table("memories").insert(data).execute()
        return response.data


supabase_memory_service = SupabaseMemoryService()
