"""Exception hierarchy. Every error carries a message safe to show a user."""

from __future__ import annotations


class BomIQError(Exception):
    """Base class for all application errors."""

    user_message = "An unexpected error occurred."

    def __init__(self, message: str = "", **context: object) -> None:
        super().__init__(message or self.user_message)
        self.context = context

    def as_dict(self) -> dict[str, object]:
        return {
            "error": type(self).__name__,
            "message": str(self),
            "context": {k: str(v) for k, v in self.context.items()},
        }


class ConfigError(BomIQError):
    user_message = "Configuration is invalid."


class CredentialError(BomIQError):
    """A provider is enabled but its credentials are missing or rejected."""

    user_message = "Provider credentials are missing or invalid."


class ReadError(BomIQError):
    """The input file could not be opened or decoded."""

    user_message = "This file could not be read."


class FormatError(ReadError):
    """The file opened but does not look like a BOM."""

    user_message = "No BOM table could be found in this file."


class MappingError(BomIQError):
    """Required columns could not be identified."""

    user_message = "Required BOM columns could not be identified."


class ProviderError(BomIQError):
    """A provider call failed. Non-fatal: the engine degrades gracefully."""

    user_message = "A data provider could not be reached."

    def __init__(self, provider: str, message: str = "",
                 retryable: bool = True, **context: object) -> None:
        super().__init__(message or f"{provider} request failed", **context)
        self.provider = provider
        self.retryable = retryable


class ExportError(BomIQError):
    user_message = "The report could not be written."


class CancelledError(BomIQError):
    """The user cancelled a running analysis."""

    user_message = "Analysis cancelled."
