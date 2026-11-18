class SearchService:
    def search(self, query):
        return {
            "status": "success",
            "query": query,
            "results": []
        }


search_service = SearchService()
