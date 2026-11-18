from backend.config import settings


class Database:
    def __init__(self):
        self.name = "OmniSight Database"

    def health(self):
        return {
            "status": "online",
            "database": self.name,
            "environment": settings.ENVIRONMENT
        }

    def tables(self):
        return [
            "people",
            "organizations",
            "projects",
            "documents",
            "memories",
            "relationships"
        ]


database = Database()
