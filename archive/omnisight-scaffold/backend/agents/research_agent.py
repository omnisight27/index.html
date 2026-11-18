class ResearchAgent:
    def analyze(self, topic):
        return {
            "agent": "research",
            "topic": topic,
            "status": "analyzed"
        }


research_agent = ResearchAgent()
