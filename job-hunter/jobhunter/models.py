"""Data types shared across the pipeline."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from enum import Enum


class Status(str, Enum):
    NEW = "new"                    # fetched, not yet scored
    REJECTED = "rejected"          # scored below threshold or excluded
    MATCHED = "matched"            # good fit, waiting for your approval
    APPROVED = "approved"          # approved, will be applied to on next `apply`
    APPLIED = "applied"            # application submitted
    DRY_RUN = "dry_run"            # form filled but not submitted (dry run)
    NEEDS_MANUAL = "needs_manual"  # captcha / unknown required question / unsupported form
    FAILED = "failed"              # unexpected error while applying
    SKIPPED = "skipped"            # you skipped it


@dataclass
class Job:
    source: str            # greenhouse | lever | ashby | remotive | remoteok
    external_id: str       # id unique within the source
    company: str
    title: str
    url: str               # human-facing posting URL
    apply_url: str = ""    # URL of the application form (if different)
    location: str = ""
    remote: bool = False
    description: str = ""  # plain text
    salary_min: int | None = None
    salary_max: int | None = None
    posted_at: str = ""
    ats: str = ""          # which applier can handle it: greenhouse | lever | ashby | ""

    # filled in by the pipeline
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    status: Status = Status.NEW

    @property
    def key(self) -> str:
        return f"{self.source}:{self.external_id}"


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")


def html_to_text(raw: str) -> str:
    """Cheap HTML -> text; good enough for keyword matching."""
    if not raw:
        return ""
    text = html.unescape(raw)          # Greenhouse double-encodes its HTML
    text = re.sub(r"<\s*(br|/p|/li|/h\d)\s*/?>", "\n", text, flags=re.I)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())
