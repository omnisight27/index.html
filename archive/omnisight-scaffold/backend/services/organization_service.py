from backend.supabase_client import supabase


class OrganizationService:
    def get_organizations(self):
        response = supabase.table("organizations").select("*").execute()
        return response.data

    def create_organization(self, data):
        response = supabase.table("organizations").insert(data).execute()
        return response.data


organization_service = OrganizationService()
