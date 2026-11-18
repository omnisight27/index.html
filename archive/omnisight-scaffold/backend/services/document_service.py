from backend.supabase_client import supabase


class DocumentService:
    def get_documents(self):
        response = supabase.table("documents").select("*").execute()
        return response.data

    def create_document(self, data):
        response = supabase.table("documents").insert(data).execute()
        return response.data


document_service = DocumentService()
