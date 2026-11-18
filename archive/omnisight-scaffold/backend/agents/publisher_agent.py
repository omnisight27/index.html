class PublisherAgent:
    def publish(self):
        return {
            "agent": "publisher",
            "status": "ready"
        }


publisher_agent = PublisherAgent()
