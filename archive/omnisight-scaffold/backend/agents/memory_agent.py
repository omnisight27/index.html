class MemoryAgent:
    def remember(self, memory):
        return {
            "agent": "memory",
            "memory": memory,
            "status": "stored"
        }


memory_agent = MemoryAgent()
