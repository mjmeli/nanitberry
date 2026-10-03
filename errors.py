"""Actionable account setup errors shared by clients and the service."""


class NanitReauthRequired(RuntimeError):
    """A fresh interactive MFA login is required."""


class UidSelectionRequired(RuntimeError):
    """An account has no unambiguous child to sync."""

