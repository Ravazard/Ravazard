import json
from pathlib import Path

import pytest
import yaml

from jobhunter import sources
from jobhunter.cli import main
from jobhunter.matcher import has_term, required_years, score_job, title_similarity
from jobhunter.models import Status, html_to_text
from jobhunter.profile import Profile, load_profile
from jobhunter.store import Store

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "fixtures"


def fake_fetch(url: str):
    if "greenhouse" in url:
        return json.loads((FIX / "greenhouse.json").read_text())
    if "lever" in url:
        return json.loads((FIX / "lever.json").read_text())
    if "ashby" in url:
        return json.loads((FIX / "ashby.json").read_text())
    raise AssertionError(url)


@pytest.fixture
def profile() -> Profile:
    return load_profile(ROOT / "profile.dba.example.yaml")


def all_jobs(profile):
    profile.sources.greenhouse = ["acme"]
    profile.sources.lever = ["globex"]
    profile.sources.ashby = ["initech"]
    return {j.external_id: j for j in sources.fetch_all(profile, fetch=fake_fetch)}


def test_example_profiles_load():
    load_profile(ROOT / "profile.example.yaml")
    load_profile(ROOT / "profile.dba.example.yaml")


def test_sources_normalize(profile):
    jobs = all_jobs(profile)
    assert set(jobs) == {"acme/101", "acme/102", "acme/103", "acme/104", "globex/abc-123", "initech/f00-1"}
    gh = jobs["acme/101"]
    assert gh.ats == "greenhouse" and gh.company == "Acme"
    assert "PostgreSQL" in gh.description and "<" not in gh.description
    lv = jobs["globex/abc-123"]
    assert lv.apply_url.endswith("/apply") and "RMAN" in lv.description and lv.salary_max == 35000
    ab = jobs["initech/f00-1"]
    assert ab.salary_min == 15000 and not ab.remote


def test_dba_scoring(profile):
    jobs = all_jobs(profile)
    scored = {k: score_job(j, profile) for k, j in jobs.items()}

    # Bengaluru PostgreSQL DBA, 2+ yrs: strong match
    assert not scored["acme/101"].excluded and scored["acme/101"].score >= 70
    # Chennai Database Engineer, 1-3 yrs: match
    assert not scored["initech/f00-1"].excluded and scored["initech/f00-1"].score >= profile.apply.min_score
    # "Principal" is too senior
    assert scored["acme/102"].excluded
    # Sales role in Bangalore: location fine but title/skills don't fit
    assert scored["acme/103"].score < profile.apply.min_score
    # Berlin: wrong city
    assert scored["acme/104"].excluded
    # "Senior Oracle DBA" in Chennai wants 5-8 years: penalised below threshold
    assert scored["globex/abc-123"].score < profile.apply.min_score


def test_matching_helpers():
    assert has_term("Expert in C++17 and STL", "C++")
    assert not has_term("C# developer", "C++")
    assert has_term("Postgres replication", "PostgreSQL")
    assert title_similarity("Sr. Oracle DBA", ["Database Administrator"]) == 1.0
    assert title_similarity("Account Executive", ["Database Administrator"]) == 0.0
    assert required_years("Need 3-5 years of experience") == 3
    assert required_years("no requirement") is None
    assert html_to_text("&lt;p&gt;Hi &amp;amp; bye&lt;/p&gt;") == "Hi & bye"


def _job(title, desc, location="Chennai"):
    from jobhunter.models import Job

    return Job(source="t", external_id=title, company="X", title=title, url="u",
               location=location, description=desc)


def test_open_source_only(profile):
    # Generic DBA title, but an Oracle-only posting: rejected (no MySQL/MariaDB).
    oracle = score_job(_job("Database Administrator", "Oracle 19c, RMAN, Data Guard, PL/SQL. 2+ years."), profile)
    assert oracle.excluded and "core skills" in oracle.reasons[0]
    # Proprietary DB in the title: rejected.
    assert score_job(_job("SQL Server DBA", "MySQL a plus"), profile).excluded
    # MongoDB-only role: you only have basic Mongo, so it's rejected.
    assert score_job(_job("Database Administrator", "MongoDB sharding and replica sets"), profile).excluded
    # MariaDB / Galera role: strong match.
    maria = score_job(_job("MariaDB DBA", "MariaDB Galera cluster, replication, backups, Linux, bash. 2-4 years."), profile)
    assert not maria.excluded and maria.score >= 80
    # Postgres-first role that also mentions MySQL: still a match, but lower than a MySQL-first one.
    pg = score_job(_job("PostgreSQL DBA", "PostgreSQL primary, some MySQL. Linux."), profile)
    my = score_job(_job("MySQL DBA", "MySQL, MariaDB, Percona, Linux, shell scripting, replication."), profile)
    assert not pg.excluded and my.score > pg.score


def test_real_false_positives_rejected(profile):
    # Titles that wrongly matched in a real run (their descriptions mention MySQL).
    desc = "We run MySQL and PostgreSQL. Linux, bash, replication."
    for title in ["Site Reliability Engineer (CI-CD)",
                  "Sales Compensation Engineer",
                  "Software Engineer – Tooling & Platform",
                  "Senior Site Reliability Engineer (CI-CD/CTAP/Delivery)",
                  "Senior MySQL DBA"]:
        assert score_job(_job(title, desc, "Bengaluru, India"), profile).excluded, title
    for title in ["MySQL DBA", "Database Administrator", "Database Engineer - MariaDB", "DB Engineer"]:
        assert not score_job(_job(title, desc, "Bengaluru, India"), profile).excluded, title


def test_vague_location_uses_description(profile):
    desc = "Available Locations: Bengaluru, India or London, UK. We run MySQL. Linux."
    assert not score_job(_job("Database Engineer", desc, "Hybrid"), profile).excluded
    desc = "Available Locations: London, UK. We run MySQL. Linux."
    assert score_job(_job("Database Engineer", desc, "In-Office"), profile).excluded
    # A real city in the location field still wins over the description.
    assert score_job(_job("Database Engineer", "Bengaluru team. MySQL.", "Seattle, WA"), profile).excluded


def test_generic_title_words_count_less():
    assert title_similarity("Site Reliability Engineer", ["Database Reliability Engineer"]) < 0.5
    assert title_similarity("Database Reliability Engineer", ["Database Reliability Engineer"]) == 1.0


def test_placeholder_values_block_applying(profile, tmp_path):
    probs = " ".join(profile.problems())
    assert "email" in probs and "first_name" in probs and "linkedin" in probs
    assert "resume not found" in probs
    (tmp_path / "resume.pdf").write_bytes(b"%PDF-1.4\n")
    profile.personal.resume_path = str(tmp_path / "resume.pdf")
    profile.personal.first_name, profile.personal.last_name = "Test", "User"
    profile.personal.email, profile.personal.phone = "me@mail.test", "+91 9000000001"
    profile.personal.linkedin = profile.personal.current_company = ""
    assert profile.problems() == []


def test_canned_answers_match_whole_words(profile):
    profile.answers = {"age": "24", "notice period": "60-90 days"}
    assert profile.canned_answer("What is your age?") == "24"
    assert profile.canned_answer("Which languages do you speak?") is None
    assert profile.canned_answer("Have you managed a team?") is None
    assert profile.canned_answer("Notice Period (days)") == "60-90 days"


def test_bangalore_alias(profile):
    jobs = all_jobs(profile)
    job = jobs["acme/101"]
    job.location = "Bangalore, IN"
    assert not score_job(job, profile).excluded


def test_store_dedupes(tmp_path, profile):
    store = Store(tmp_path / "j.db")
    job = all_jobs(profile)["acme/101"]
    assert store.upsert(job) is True
    store.set_status(job.key, Status.APPLIED)
    assert store.upsert(job) is False              # re-fetch doesn't reset status
    assert store.get(job.key).status is Status.APPLIED


def test_cli_search_list_approve(tmp_path, monkeypatch, capsys, profile):
    data = yaml.safe_load((ROOT / "profile.dba.example.yaml").read_text())
    data["sources"] = {"greenhouse": ["acme"], "lever": ["globex"], "ashby": ["initech"]}
    prof = tmp_path / "profile.yaml"
    prof.write_text(yaml.safe_dump(data))
    db = str(tmp_path / "jobs.db")
    monkeypatch.setattr(sources, "http_get_json", fake_fetch)

    assert main(["--profile", str(prof), "--db", db, "search"]) == 0
    out = capsys.readouterr().out
    assert "6 new jobs, 2 good matches" in out

    assert main(["--profile", str(prof), "--db", db, "list", "--status", "rejected",
                 "--title", "oracle", "database", "--why"]) == 0
    out = capsys.readouterr().out
    assert "Senior Oracle DBA" in out and "Principal Database Architect" in out
    assert "why: title contains excluded" in out
    assert "Account Executive" not in out and "SQL Server DBA" not in out

    assert main(["--profile", str(prof), "--db", db, "approve", "--top", "5"]) == 0
    store = Store(db)
    approved = store.by_status(Status.APPROVED)
    assert {j.key for j in approved} == {"greenhouse:acme/101", "ashby:initech/f00-1"}
