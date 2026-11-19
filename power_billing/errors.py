"""Typed errors for the power / demand-billing libraries."""

from __future__ import annotations


class PowerBillingError(Exception):
    """Base class for all library errors."""


class ValidationError(PowerBillingError):
    """Input failed structural validation."""


class SegmentationError(ValidationError):
    """A power trajectory is malformed (overlap, bad length, duplicate id, ...)."""


class ContractError(ValidationError):
    """A contract is malformed (period, window, included demand, price)."""


class RevisionError(ValidationError):
    """A power revision is malformed or conflicts with the current trajectory."""


class InsufficientEvidence(PowerBillingError):
    """No fully-known window exists, so no successful charge can be produced."""


class ConflictError(PowerBillingError):
    """An idempotency, version, predecessor, or concurrency check failed."""


class NotFoundError(PowerBillingError):
    """A referenced object does not exist."""
