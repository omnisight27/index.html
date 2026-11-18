from backend.supabase_client import supabase


class SupabasePeopleService:
    def get_people(self):
        response = supabase.table("people").select("*").execute()
        return response.data


supabase_people_service = SupabasePeopleService()
