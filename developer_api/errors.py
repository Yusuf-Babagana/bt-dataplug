class ApiError(Exception):
    """Raised anywhere in the developer API; rendered as the standard error envelope."""

    def __init__(self, status, code, message, details=None, headers=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers or {}


def validation_error(**fields):
    return ApiError(422, 'validation_error', 'One or more fields are invalid.', details=fields)
