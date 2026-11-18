from backend.supabase_client import supabase


class ProjectService:
    def get_projects(self):
        response = supabase.table("projects").select("*").execute()
        return response.data

    def create_project(self, data):
        response = supabase.table("projects").insert(data).execute()
        return response.data


project_service = ProjectService()
