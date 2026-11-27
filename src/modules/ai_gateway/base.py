from __future__ import annotations


class GatewayError(Exception):
    """A public, credential-free error that can be returned to the caller."""

    def __init__(self, message: str, status_code: int = 400, code: str = "gateway_error"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class UnavailableProvider:
    def __init__(self, provider_id: str, name: str, reason: str):
        self.id, self.name = provider_id, name
        self.capabilities = dict(login=False, local_import=False, chat=False, checkin=False, reason=reason)
