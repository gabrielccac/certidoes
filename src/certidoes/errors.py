from __future__ import annotations


class CertificateError(RuntimeError):
    """Expected source failure with a stable stage/code for the API boundary."""

    def __init__(
        self,
        source: str,
        stage: str,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        upstream_status: int | None = None,
    ) -> None:
        self.source = source
        self.stage = stage
        self.code = code
        self.message = message
        self.retryable = retryable
        self.upstream_status = upstream_status
        super().__init__(message)
