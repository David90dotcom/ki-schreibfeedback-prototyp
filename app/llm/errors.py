from __future__ import annotations

from typing import Any

from app.domain.metrics import ErrorType


class ProviderError(RuntimeError):
    """Basisklasse für alle providerunabhängig erfassbaren Fehler."""

    error_type: ErrorType = ErrorType.UNKNOWN
    default_retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        provider_id: str,
        model_name: str | None = None,
        status_code: int | None = None,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)

        self.message = message
        self.provider_id = provider_id
        self.model_name = model_name
        self.status_code = status_code
        self.retryable = (
            self.default_retryable
            if retryable is None
            else retryable
        )
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        """Liefert eine speicherbare, providerunabhängige Fehlerdarstellung."""

        return {
            "error_type": self.error_type.value,
            "message": self.message,
            "provider_id": self.provider_id,
            "model_name": self.model_name,
            "status_code": self.status_code,
            "retryable": self.retryable,
            "details": self.details,
        }


class ProviderAuthenticationError(ProviderError):
    error_type = ErrorType.AUTHENTICATION


class ProviderAuthorizationError(ProviderError):
    error_type = ErrorType.AUTHORIZATION


class ProviderRateLimitError(ProviderError):
    error_type = ErrorType.RATE_LIMIT
    default_retryable = True


class ProviderModelNotFoundError(ProviderError):
    error_type = ErrorType.MODEL_NOT_FOUND


class ProviderInvalidRequestError(ProviderError):
    error_type = ErrorType.INVALID_REQUEST


class ProviderConnectionError(ProviderError):
    error_type = ErrorType.CONNECTION
    default_retryable = True


class ProviderTimeoutError(ProviderError):
    error_type = ErrorType.TIMEOUT
    default_retryable = True


class ProviderUnavailableError(ProviderError):
    error_type = ErrorType.PROVIDER_UNAVAILABLE
    default_retryable = True


class ProviderInvalidResponseError(ProviderError):
    error_type = ErrorType.INVALID_RESPONSE


class ProviderStructuredOutputError(ProviderError):
    error_type = ErrorType.STRUCTURED_OUTPUT


class ProviderCancelledError(ProviderError):
    error_type = ErrorType.CANCELLED


class ProviderUnknownError(ProviderError):
    error_type = ErrorType.UNKNOWN


def provider_error_from_http_status(
    *,
    provider_id: str,
    model_name: str | None,
    status_code: int,
    message: str,
    details: dict[str, Any] | None = None,
) -> ProviderError:
    """Ordnet einen HTTP-Status einer einheitlichen Fehlerklasse zu."""

    common_arguments = {
        "provider_id": provider_id,
        "model_name": model_name,
        "status_code": status_code,
        "details": details,
    }

    if status_code == 400 or status_code == 422:
        return ProviderInvalidRequestError(
            message,
            **common_arguments,
        )

    if status_code == 401:
        return ProviderAuthenticationError(
            message,
            **common_arguments,
        )

    if status_code == 403:
        return ProviderAuthorizationError(
            message,
            **common_arguments,
        )

    if status_code == 404:
        return ProviderModelNotFoundError(
            message,
            **common_arguments,
        )

    if status_code == 408:
        return ProviderTimeoutError(
            message,
            **common_arguments,
        )

    if status_code == 429:
        return ProviderRateLimitError(
            message,
            **common_arguments,
        )

    if 500 <= status_code <= 599:
        return ProviderUnavailableError(
            message,
            **common_arguments,
        )

    return ProviderUnknownError(
        message,
        **common_arguments,
    )