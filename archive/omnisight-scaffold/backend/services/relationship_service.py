from backend.supabase_client import supabase


class RelationshipService:
    def get_relationships(self):
        response = supabase.table("relationships").select("*").execute()
        return response.data

    def create_relationship(self, data):
        response = supabase.table("relationships").insert(data).execute()
        return response.data


relationship_service = RelationshipService()
