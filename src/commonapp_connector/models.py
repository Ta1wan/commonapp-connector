"""Normalized record types produced by the connector."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any


@dataclass
class Applicant:
    common_app_id: str
    first_name: str = ""
    last_name: str = ""
    preferred_name: str = ""
    email: str = ""
    phone: str = ""
    birth_date: date | None = None
    address_line1: str = ""
    address_line2: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    country: str = ""
    citizenship: str = ""
    high_school_ceeb: str = ""
    high_school_name: str = ""
    graduation_date: date | None = None


@dataclass
class Application:
    applicant: Applicant
    application_id: str = ""
    term: str = ""
    decision_plan: str = ""
    intended_major: str = ""
    submitted_at: str = ""
    fee_status: str = ""
    # Columns present in the export that the field map does not claim.
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Stable identity for de-duplication: one record per applicant per term."""
        return f"{self.applicant.common_app_id}:{self.term or self.application_id}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for k in ("birth_date", "graduation_date"):
            v = data["applicant"][k]
            data["applicant"][k] = v.isoformat() if v else None
        return data
