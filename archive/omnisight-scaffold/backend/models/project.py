class Project:
    def __init__(
        self,
        name,
        description=None,
        status="active"
    ):
        self.name = name
        self.description = description
        self.status = status
