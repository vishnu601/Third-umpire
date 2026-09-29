"""The one refusal type: a rule said no, with the HTTP status and a stable code."""


class Refused(Exception):
    def __init__(self, status, code, message=""):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code.replace("_", " ")
