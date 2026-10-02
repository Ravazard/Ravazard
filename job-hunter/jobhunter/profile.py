"""Load and validate the user's profile.yaml."""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class Personal(BaseModel):
    first_name: str
    last_name: str
    email: str
    phone: str = ""
    location: str = ""
    linkedin: str = ""
    github: str = ""
    website: str = ""
    resume_path: str = ""
    current_company: str = ""
    years_experience: float = 0
    work_authorization: str = ""
    requires_sponsorship: bool = False

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class Search(BaseModel):
    titles: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    remote_only: bool = False
    include_remote: bool = True      # also accept remote jobs outside your locations
    require_core_skill: bool = False  # reject postings that mention none of your core skills
    required_title_words: list[str] = Field(default_factory=list)  # title must contain one of these
    locations: list[str] = Field(default_factory=list)
    min_salary_usd: int = 0
    exclude_title_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    exclude_companies: list[str] = Field(default_factory=list)


class Skills(BaseModel):
    core: list[str] = Field(default_factory=list)    # strongest skills, weight 2
    other: list[str] = Field(default_factory=list)   # solid working knowledge, weight 1
    basic: list[str] = Field(default_factory=list)   # familiarity only, weight 0.5
    avoid: list[str] = Field(default_factory=list)   # tech you don't want; -12 points each


class Sources(BaseModel):
    greenhouse: list[str] = Field(default_factory=list)
    lever: list[str] = Field(default_factory=list)
    ashby: list[str] = Field(default_factory=list)
    remotive: bool = False
    remoteok: bool = False


class ApplySettings(BaseModel):
    min_score: int = 60
    auto_approve: bool = False
    dry_run: bool = True
    max_per_run: int = 10
    headless: bool = True
    use_llm_for_cover_letter: bool = False
    use_llm_for_questions: bool = False


class Notify(BaseModel):
    email_to: str = ""                   # where the nightly digest goes
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""                  # defaults to email_to
    max_description_chars: int = 6000    # per job, in the email


class Profile(BaseModel):
    personal: Personal
    search: Search = Field(default_factory=Search)
    skills: Skills = Field(default_factory=Skills)
    sources: Sources = Field(default_factory=Sources)
    apply: ApplySettings = Field(default_factory=ApplySettings)
    notify: Notify = Field(default_factory=Notify)
    # A value can be a list of acceptable answers; the first one a dropdown offers is used.
    answers: dict[str, str | list[str]] = Field(default_factory=dict)
    cover_letter_template: str = ""

    # Directory profile.yaml lives in; relative paths resolve against it.
    base_dir: Path = Field(default=Path("."), exclude=True)

    @property
    def resume_file(self) -> Path | None:
        if not self.personal.resume_path:
            return None
        p = Path(self.personal.resume_path).expanduser()
        return p if p.is_absolute() else (self.base_dir / p).resolve()

    def problems(self) -> list[str]:
        """Things that must be fixed before real applications go out."""
        out = []
        p = self.personal
        if not p.email or "example.com" in p.email:
            out.append("personal.email is not set")
        resume = self.resume_file
        if not resume or not resume.exists():
            out.append(f"resume not found at {resume} (copy your resume there as resume.pdf)")
        for name in ("first_name", "last_name", "email", "phone", "linkedin", "current_company"):
            value = getattr(p, name)
            if re.search(r"your-handle|example\.com|^your\b|^name$|fill.?me|90000 00000|555 000", value, re.I):
                out.append(f"personal.{name} still has the example value '{value}'")
        return sorted(set(out))

    def canned_answer(self, question: str) -> str | list[str] | None:
        """Return a configured answer whose key appears (as whole words) in the question label."""
        q = question.lower()
        # Longest key first so "authorized to work" beats "work".
        for key in sorted(self.answers, key=len, reverse=True):
            # Whole words only, so "age" doesn't match "language" or "manage".
            if re.search(rf"(?<![a-z0-9]){re.escape(key.lower())}(?![a-z0-9])", q):
                return self.answers[key]
        return None

    def cover_letter(self, company: str, title: str) -> str:
        top = (self.skills.core + self.skills.other)[:3]
        return self.cover_letter_template.format(
            company=company,
            title=title,
            years=int(self.personal.years_experience),
            top_skills=", ".join(top) if top else "software engineering",
            first_name=self.personal.first_name,
            last_name=self.personal.last_name,
        ).strip()


def load_profile(path: str | Path) -> Profile:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy profile.example.yaml to {path.name} and fill it in."
        )
    data = yaml.safe_load(path.read_text()) or {}
    profile = Profile.model_validate(data)
    profile.base_dir = path.parent.resolve()
    return profile
