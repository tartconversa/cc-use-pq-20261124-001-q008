class DomainError(Exception):
    def __init__(self, status: int, code: str, message: str, details=None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details

    def payload(self):
        result = {"code": self.code, "message": self.message}
        if self.details is not None:
            result["details"] = self.details
        return result


def missing(kind: str, identifier: str) -> DomainError:
    return DomainError(404, f"{kind}_missing", f"未找到{kind}: {identifier}")


def conflict(code: str, message: str, details=None) -> DomainError:
    return DomainError(409, code, message, details)


def invalid(code: str, message: str, details=None) -> DomainError:
    return DomainError(422, code, message, details)

