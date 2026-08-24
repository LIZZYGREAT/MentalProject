"""Shared warning delivery configuration."""

from dataclasses import dataclass


@dataclass(frozen=True)
class WarningDeliveryPolicyConfig:
    max_daily_sends: int
    min_interval_minutes: int

    def __post_init__(self) -> None:
        if self.max_daily_sends < 0:
            raise ValueError("max_daily_sends must be non-negative")
        if self.min_interval_minutes < 0:
            raise ValueError("min_interval_minutes must be non-negative")

    def identity_payload(self) -> dict[str, int]:
        return {
            "max_daily_sends": self.max_daily_sends,
            "min_interval_minutes": self.min_interval_minutes,
        }
