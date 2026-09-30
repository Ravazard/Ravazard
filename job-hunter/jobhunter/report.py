"""Which skills do real database job postings ask for? Counted from the jobs already fetched."""

from __future__ import annotations

from dataclasses import dataclass

from jobhunter.matcher import has_term, required_years
from jobhunter.models import Job
from jobhunter.profile import Profile

# skill -> extra spellings (on top of matcher.ALIASES)
CATALOG: dict[str, list[str]] = {
    "MySQL": [], "MariaDB": [], "PostgreSQL": [], "MongoDB": [], "Redis": [], "Cassandra": [],
    "Oracle": [], "SQL Server": [],
    "Replication": ["replica", "replicas"], "GTID": [],
    "Group Replication / InnoDB Cluster": ["group replication", "innodb cluster"],
    "Galera": ["galera cluster"], "ProxySQL": [], "Orchestrator": [], "Vitess": [],
    "Percona XtraBackup": [], "Percona Toolkit": ["pt-query-digest", "pt-online-schema-change", "percona toolkit"],
    "Online schema change": ["gh-ost", "pt-online-schema-change", "online schema change"],
    "Backup and Recovery": [], "Performance Tuning": [], "High Availability": [],
    "Disaster Recovery": ["dr drills", "disaster recovery"], "Sharding": ["sharded", "shard"],
    "AWS RDS": [], "AWS": ["amazon web services"], "Azure": [], "GCP": ["google cloud", "cloud sql"],
    "Linux": [], "Shell Scripting": [], "Python": [], "Go": ["golang"],
    "Ansible": [], "Terraform": [], "Kubernetes": [], "Docker": ["containers"],
    "Prometheus": [], "Grafana": [], "PMM": ["percona monitoring and management"],
    "CI/CD": ["ci/cd", "github actions", "jenkins", "gitlab ci"], "Security": ["encryption", "rbac"],
    "On-call": ["on-call", "oncall", "24x7", "24/7"],
}


@dataclass
class SkillCount:
    skill: str
    count: int
    share: float
    yours: bool


def database_jobs(jobs: list[Job], profile: Profile, max_years: int | None) -> list[Job]:
    words = profile.search.required_title_words or ["database", "dba", "db", "mysql", "postgres", "sql"]
    out = []
    for j in jobs:
        if not any(has_term(j.title, w) for w in words):
            continue
        yrs = required_years(j.description)
        if max_years is not None and yrs is not None and yrs > max_years:
            continue
        out.append(j)
    return out


def _mentions(text: str, skill: str) -> bool:
    return has_term(text, skill) or any(has_term(text, a) for a in CATALOG.get(skill, []))


def skill_counts(jobs: list[Job], profile: Profile) -> list[SkillCount]:
    mine = {s.lower() for s in profile.skills.core + profile.skills.other + profile.skills.basic}
    rows = []
    for skill in CATALOG:
        n = sum(1 for j in jobs if _mentions(f"{j.title}\n{j.description}", skill))
        rows.append(SkillCount(skill, n, n / len(jobs) if jobs else 0.0, skill.lower() in mine))
    return sorted(rows, key=lambda r: -r.count)
