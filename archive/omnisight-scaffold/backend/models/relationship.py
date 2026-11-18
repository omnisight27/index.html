class Relationship:
    def __init__(
        self,
        source_type,
        source_id,
        target_type,
        target_id,
        relationship_type
    ):
        self.source_type = source_type
        self.source_id = source_id
        self.target_type = target_type
        self.target_id = target_id
        self.relationship_type = relationship_type
