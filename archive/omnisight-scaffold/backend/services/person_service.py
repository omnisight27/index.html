from backend.supabase_client import supabase


class PersonService:
    def get_people(self):
        response = supabase.table("people").select("*").execute()
        return response.data

    def create_person(self, data):
        response = supabase.table("people").insert(data).execute()
        return response.data


person_service = PersonService()
