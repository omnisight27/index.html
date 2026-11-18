class Document:
    def __init__(
        self,
        title,
        file_name=None,
        file_type=None,
        source=None,
        content=None
    ):
        self.title = title
        self.file_name = file_name
        self.file_type = file_type
        self.source = source
        self.content = content
