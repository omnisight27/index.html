class LearningService:

    def calculate_accuracy(
        self,
        expected,
        actual
    ):
        if expected == actual:
            return 100

        return 0

    def update_confidence(
        self,
        confidence,
        success_score
    ):
        if success_score >= 80:
            confidence += 5

        elif success_score <= 40:
            confidence -= 5

        return max(
            0,
            min(100, confidence)
        )


learning_service = LearningService()
