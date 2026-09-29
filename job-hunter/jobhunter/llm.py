"""Optional Claude-powered helpers: fit re-ranking, cover letters, screening answers.

Everything here is opt-in (see the `apply:` section of profile.yaml and the
`--llm` flag) and needs ANTHROPIC_API_KEY (or an `ant auth login` profile).
The rest of the tool works without it.

Refusal fallbacks are enabled (`fallbacks="default"`): if Claude's safety
classifiers decline a request, the API transparently retries it on
Anthropic's recommended fallback model instead of returning an empty answer.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from pydantic import BaseModel, Field

from jobhunter.models import Job
from jobhunter.profile import Profile

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
BETAS = ["server-side-fallback-2026-07-01"]

SYSTEM = (
    "You help a job seeker evaluate postings and fill out job applications. "
    "Only state facts that appear in the candidate profile you are given. Never "
    "invent experience, degrees, employers, dates or skills. If the profile does "
    "not contain the information needed, say so instead of guessing."
)

# Only the job description is sent (truncated) to keep costs predictable.
MAX_DESC_CHARS = 12_000


class FitAssessment(BaseModel):
    score: int = Field(description="0-100: how well the candidate fits this role")
    summary: str = Field(description="One or two sentences explaining the score")
    missing_skills: list[str] = Field(description="Important requirements the candidate lacks")


class CoverLetter(BaseModel):
    text: str


class QuestionAnswer(BaseModel):
    can_answer: bool = Field(description="False if the profile doesn't contain enough information")
    answer: str = Field(description="The answer to enter in the form, or empty if can_answer is false")


@lru_cache(maxsize=1)
def _client():
    import anthropic  # imported lazily so the tool runs without the SDK installed

    return anthropic.Anthropic()


def _profile_block(profile: Profile) -> str:
    p = profile.model_dump(exclude={"base_dir", "sources", "apply", "cover_letter_template"})
    # Contact details aren't needed for reasoning about fit.
    for k in ("email", "phone", "resume_path"):
        p["personal"].pop(k, None)
    import yaml

    return yaml.safe_dump(p, sort_keys=False)


def _job_block(job: Job) -> str:
    return (
        f"Company: {job.company}\nTitle: {job.title}\nLocation: {job.location}"
        f"{' (remote)' if job.remote else ''}\n\n{job.description[:MAX_DESC_CHARS]}"
    )


def _ask(prompt: str, schema: type[BaseModel], effort: str, max_tokens: int = 4000):
    resp = _client().beta.messages.parse(
        model=MODEL,
        max_tokens=max_tokens,
        betas=BETAS,
        fallbacks="default",
        system=SYSTEM,
        output_config={"effort": effort},
        output_format=schema,
        messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined this request")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Claude's response was cut off (max_tokens)")
    return resp.parsed_output


def assess_fit(job: Job, profile: Profile) -> FitAssessment:
    prompt = (
        f"<candidate_profile>\n{_profile_block(profile)}</candidate_profile>\n\n"
        f"<job_posting>\n{_job_block(job)}\n</job_posting>\n\n"
        "Rate how well this candidate fits the role (0-100). Weigh must-have "
        "requirements, seniority and location heavily; nice-to-haves lightly."
    )
    return _ask(prompt, FitAssessment, effort="low")


def write_cover_letter(job: Job, profile: Profile) -> str:
    prompt = (
        f"<candidate_profile>\n{_profile_block(profile)}</candidate_profile>\n\n"
        f"<job_posting>\n{_job_block(job)}\n</job_posting>\n\n"
        "Write a concise cover letter (under 200 words, plain text, no placeholders) "
        "for this role. Connect the candidate's real skills to the posting's needs. "
        f"Sign it as {profile.personal.full_name}."
    )
    return _ask(prompt, CoverLetter, effort="medium").text.strip()


def answer_question(question: str, options: list[str], job: Job, profile: Profile) -> str | None:
    opts = ("\nChoose exactly one of these options:\n- " + "\n- ".join(options)) if options else ""
    prompt = (
        f"<candidate_profile>\n{_profile_block(profile)}</candidate_profile>\n\n"
        f"Applying to: {job.title} at {job.company}\n\n"
        f"Application form question: {question}{opts}\n\n"
        "Answer as the candidate, briefly. If the profile doesn't give you what you "
        "need to answer truthfully, set can_answer to false."
    )
    try:
        qa = _ask(prompt, QuestionAnswer, effort="low", max_tokens=2000)
    except Exception as exc:  # noqa: BLE001 - an unanswered question just means manual review
        log.warning("LLM could not answer %r: %s", question, exc)
        return None
    if not qa.can_answer or not qa.answer.strip():
        return None
    if options and qa.answer not in options:
        return None
    return qa.answer.strip()
