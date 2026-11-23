from fastapi.responses import JSONResponse


class DomainError(Exception):
    def __init__(self, code, message, status=422, details=None):
        self.code = code
        self.message = message
        self.status = status
        self.details = details


async def respond(_request, error):
    body = {"code": error.code, "message": error.message}
    if error.details is not None:
        body["details"] = error.details
    return JSONResponse(body, status_code=error.status)
