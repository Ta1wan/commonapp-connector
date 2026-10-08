"""Maps export column headers onto model fields.

Common App member export templates are configured per institution, so column
headers vary. DEFAULT_FIELD_MAP covers common header spellings; override or
extend it with a JSON file (see README) to match your own template.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# model path -> accepted header aliases (compared after normalization)
DEFAULT_FIELD_MAP: dict[str, list[str]] = {
    "applicant.common_app_id": ["common app id", "commonapp id", "cas id", "applicant id", "ca id"],
    "applicant.first_name": ["first name", "first/given name", "given name", "legal first name"],
    "applicant.last_name": ["last name", "last/family/surname", "family name", "surname", "legal last name"],
    "applicant.preferred_name": ["preferred name", "nickname"],
    "applicant.email": ["email", "email address", "preferred email"],
    "applicant.phone": ["phone", "preferred phone", "phone number", "mobile phone"],
    "applicant.birth_date": ["date of birth", "birth date", "dob"],
    "applicant.address_line1": ["address line 1", "permanent address 1", "street address", "address 1"],
    "applicant.address_line2": ["address line 2", "permanent address 2", "address 2"],
    "applicant.city": ["city", "permanent city"],
    "applicant.state": ["state", "state/province", "permanent state"],
    "applicant.postal_code": ["zip", "zip code", "postal code", "permanent zip"],
    "applicant.country": ["country", "permanent country"],
    "applicant.citizenship": ["citizenship status", "citizenship"],
    "applicant.high_school_ceeb": ["ceeb", "school ceeb", "high school ceeb", "current school ceeb"],
    "applicant.high_school_name": ["school name", "high school", "current school name"],
    "applicant.graduation_date": ["graduation date", "hs graduation date", "expected graduation date"],
    "application_id": ["application id", "app id"],
    "term": ["entry term", "term", "start term"],
    "decision_plan": ["decision plan", "admission plan", "plan"],
    "intended_major": ["intended major", "academic interest", "major"],
    "submitted_at": ["submitted date", "submission date", "date submitted"],
    "fee_status": ["fee status", "payment status", "app fee status"],
}

_NORMALIZE_RE = re.compile(r"[\s_\-]+")


def normalize_header(header: str) -> str:
    return _NORMALIZE_RE.sub(" ", header.strip().lstrip("﻿").lower()).strip()


def load_field_map(path: str | Path | None = None) -> dict[str, list[str]]:
    """Return the default map, with entries from ``path`` replacing matching keys."""
    field_map = {k: list(v) for k, v in DEFAULT_FIELD_MAP.items()}
    if path:
        overrides = json.loads(Path(path).read_text())
        for key, aliases in overrides.items():
            if key not in field_map:
                raise ValueError(f"Unknown field in field map: {key!r}")
            field_map[key] = [aliases] if isinstance(aliases, str) else list(aliases)
    return field_map


def resolve_columns(headers: list[str], field_map: dict[str, list[str]]) -> dict[str, int]:
    """Return model path -> column index for every field found in ``headers``."""
    index = {normalize_header(h): i for i, h in enumerate(headers)}
    resolved: dict[str, int] = {}
    for path, aliases in field_map.items():
        for alias in aliases:
            i = index.get(normalize_header(alias))
            if i is not None:
                resolved[path] = i
                break
    return resolved
