class SearchAgent:
    def search(self, query):
        return {
            "agent": "search",
            "query": query,
            "status": "complete"
        }


search_agent = SearchAgent()
