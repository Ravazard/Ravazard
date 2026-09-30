"""Score how well a job fits the profile (0-100), with human-readable reasons.

Rule-based and deterministic so it's free, fast and explainable. The optional
LLM pass in llm.py can re-rank the top matches afterwards.

Score breakdown:
    title match        up to 40
    skills overlap     up to 40
    location fit          15
    experience fit         5   (and -20 if the job wants far more experience)
Hard excludes (company, title keyword, keyword, location, salary) -> score 0.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jobhunter.models import Job
from jobhunter.profile import Profile

# Extra spellings that should count as the same skill.
ALIASES: dict[str, list[str]] = {
    "c++": ["cpp", "c plus plus", "c++11", "c++14", "c++17", "c++20"],
    "competitive programming": ["codeforces", "leetcode", "icpc", "topcoder", "codechef"],
    "algorithms": ["algorithm", "algorithmic"],
    "data structures": ["data structure"],
    "javascript": ["js", "node.js", "nodejs"],
    "typescript": ["ts"],
    "go": ["golang"],
    "postgresql": ["postgres"],
    "mysql": ["percona", "innodb", "aurora mysql"],
    "mariadb": ["maria db"],
    "mongodb": ["mongo"],
    "arangodb": ["arango"],
    "sql server": ["mssql", "ms sql", "microsoft sql server"],
    "oracle": ["oracle 19c", "oracle 12c", "oracle db", "oracle database"],
    "backup and recovery": ["backup & recovery", "backups", "rman", "point-in-time recovery"],
    "performance tuning": ["query optimization", "query tuning", "sql tuning"],
    "high availability": ["ha", "always on", "data guard", "rac", "failover"],
    "shell scripting": ["bash", "shell script"],
    "kubernetes": ["k8s"],
    "machine learning": ["ml"],
}

# Cities that go by more than one name on job boards.
LOCATION_ALIASES: dict[str, list[str]] = {
    "bengaluru": ["bangalore", "blr"],
    "bangalore": ["bengaluru", "blr"],
    "chennai": ["madras"],
    "gurugram": ["gurgaon"],
    "mumbai": ["bombay"],
}


def location_matches(job_location: str, wanted: list[str]) -> bool:
    loc = job_location.lower()
    for w in wanted:
        names = {w.lower(), *LOCATION_ALIASES.get(w.lower(), [])}
        if any(n in loc for n in names):
            return True
    return False


TITLE_SYNONYMS = {
    "dba": "database administrator",
    "db": "database",
    "developer": "engineer",
    "dev": "engineer",
    "programmer": "engineer",
    "swe": "software engineer",
    "sde": "software engineer",
    "back-end": "backend",
    "back end": "backend",
    "front-end": "frontend",
    "front end": "frontend",
    "full-stack": "fullstack",
    "full stack": "fullstack",
}

_WORD_CHARS = r"A-Za-z0-9+#"


def _term_regex(term: str) -> re.Pattern[str]:
    # Word boundaries that understand "C++" and "C#" (\b does not).
    return re.compile(rf"(?<![{_WORD_CHARS}]){re.escape(term)}(?![{_WORD_CHARS}])", re.I)


def has_term(text: str, skill: str) -> bool:
    variants = {skill.lower(), *ALIASES.get(skill.lower(), [])}
    return any(_term_regex(v).search(text) for v in variants)


def _norm_title(title: str) -> set[str]:
    t = title.lower()
    for src, dst in TITLE_SYNONYMS.items():
        t = re.sub(rf"(?<![a-z]){re.escape(src)}(?![a-z])", dst, t)
    t = re.sub(r"\b(sr|senior|jr|junior|mid|level|i{1,3}|iv|\d+)\b", " ", t)
    return {w for w in re.findall(r"[a-z+#]+", t) if w not in {"and", "of", "the", "a", "-"}}


# Words that appear in almost every tech title; sharing only these says little.
GENERIC_TITLE_WORDS = {"engineer", "software", "specialist", "analyst", "associate",
                       "consultant", "reliability", "operations", "platform", "systems"}


def title_similarity(title: str, targets: list[str]) -> float:
    """Weighted word overlap; generic words like "engineer" count a quarter."""
    words = _norm_title(title)

    def weight(w: str) -> float:
        return 0.25 if w in GENERIC_TITLE_WORDS else 1.0

    best = 0.0
    for target in targets:
        want = _norm_title(target)
        if want:
            got = sum(weight(w) for w in words & want)
            best = max(best, got / sum(weight(w) for w in want))
    return best


_YEARS_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?(?:years|yrs)", re.I)


def required_years(text: str) -> int | None:
    found = [int(n) for n in _YEARS_RE.findall(text) if 0 < int(n) <= 15]
    return min(found) if found else None


@dataclass
class Match:
    score: float
    reasons: list[str] = field(default_factory=list)
    excluded: bool = False


def score_job(job: Job, profile: Profile) -> Match:
    s = profile.search
    text = f"{job.title}\n{job.description}"
    reasons: list[str] = []

    # ---- hard excludes -------------------------------------------------------
    if any(c.lower() == job.company.lower() for c in s.exclude_companies):
        return Match(0, [f"excluded company {job.company}"], True)
    for kw in s.exclude_title_keywords:
        if has_term(job.title, kw):
            return Match(0, [f"title contains excluded '{kw}'"], True)
    if s.required_title_words and not any(has_term(job.title, w) for w in s.required_title_words):
        return Match(0, ["title has none of: " + ", ".join(s.required_title_words)], True)
    for kw in s.exclude_keywords:
        if has_term(text, kw):
            return Match(0, [f"posting mentions excluded '{kw}'"], True)
    if s.remote_only and not job.remote:
        return Match(0, ["not remote"], True)
    if job.remote and not s.include_remote and not location_matches(job.location, s.locations):
        return Match(0, ["remote job, but include_remote is off"], True)
    if s.locations and not (job.remote and s.include_remote):
        if not location_matches(job.location, s.locations):
            return Match(0, [f"location '{job.location}' not in your locations"], True)
    if s.min_salary_usd and job.salary_max and job.salary_max < s.min_salary_usd:
        return Match(0, [f"max salary {job.salary_max} < your minimum {s.min_salary_usd}"], True)

    score = 0.0

    # ---- title ---------------------------------------------------------------
    t = title_similarity(job.title, s.titles) if s.titles else 1.0
    score += 40 * t
    reasons.append(f"title match {t:.0%}")

    # ---- skills --------------------------------------------------------------
    weights: dict[str, float] = {sk: 0.5 for sk in profile.skills.basic}
    weights |= {sk: 1 for sk in profile.skills.other}
    weights |= {sk: 2 for sk in profile.skills.core}
    matched = [sk for sk in weights if has_term(text, sk)]
    if s.require_core_skill and profile.skills.core and not any(
        sk in profile.skills.core for sk in matched
    ):
        return Match(0, ["none of your core skills (" + ", ".join(profile.skills.core) + ") mentioned"], True)
    if weights:
        got = sum(weights[sk] for sk in matched)
        # Saturate: a posting rarely lists *all* of your skills.
        target = max(4, 0.5 * sum(weights.values()))
        score += 40 * min(1.0, got / target)
    reasons.append("skills: " + (", ".join(matched) if matched else "none matched"))

    # ---- location ------------------------------------------------------------
    score += 15
    reasons.append("remote" if job.remote else f"location ok ({job.location})")

    # ---- experience ----------------------------------------------------------
    yrs = required_years(job.description)
    have = profile.personal.years_experience
    if yrs is None:
        score += 5
    elif yrs <= have + 1:
        score += 5
        reasons.append(f"asks {yrs}+ yrs (you have {have:g})")
    elif yrs > have + 2:
        score -= 20
        reasons.append(f"asks {yrs}+ yrs, you have {have:g}")

    return Match(round(max(0.0, min(100.0, score)), 1), reasons)
