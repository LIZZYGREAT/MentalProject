"""Per-job cumulative budgets for the public research runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import uuid

from .contracts import ResearchJobSpec


class ResearchBudgetExceeded(ValueError):
    def __init__(self, operation: str) -> None:
        super().__init__("research_budget_exhausted")
        self.operation = operation


@dataclass
class ResearchJobSession:
    job_id: str
    deadline_at: datetime
    max_searches: int
    max_pages: int
    max_browser_pages: int
    max_exec_calls: int
    search_count: int = 0
    page_count: int = 0
    browser_count: int = 0
    exec_count: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def from_spec(cls, spec: ResearchJobSpec) -> "ResearchJobSession":
        now = datetime.now(timezone.utc)
        return cls(
            job_id=spec.job_id or uuid.uuid4().hex,
            deadline_at=now + timedelta(seconds=spec.deadline_seconds),
            max_searches=spec.max_searches,
            max_pages=spec.max_pages,
            max_browser_pages=spec.max_browser_pages,
            max_exec_calls=spec.max_exec_calls,
            created_at=now,
        )

    def consume(self, operation: str, amount: int = 1) -> None:
        now = datetime.now(timezone.utc)
        if now >= self.deadline_at:
            raise ResearchBudgetExceeded("deadline")
        if operation == "search":
            if self.search_count + amount > self.max_searches:
                raise ResearchBudgetExceeded(operation)
            self.search_count += amount
        elif operation == "page":
            if self.page_count + amount > self.max_pages:
                raise ResearchBudgetExceeded(operation)
            self.page_count += amount
        elif operation == "browser":
            if self.browser_count + amount > self.max_browser_pages:
                raise ResearchBudgetExceeded(operation)
            self.browser_count += amount
        elif operation == "exec":
            if self.exec_count + amount > self.max_exec_calls:
                raise ResearchBudgetExceeded(operation)
            self.exec_count += amount
        else:
            raise ValueError("unsupported research budget operation")

    def as_dict(self) -> dict[str, int | str]:
        return {
            "job_id": self.job_id,
            "created_at": self.created_at.isoformat(),
            "deadline_at": self.deadline_at.isoformat(),
            "search_count": self.search_count,
            "page_count": self.page_count,
            "browser_count": self.browser_count,
            "exec_count": self.exec_count,
        }
