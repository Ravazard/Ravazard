"""Fetch job postings from public job-board APIs.

Only official, public, unauthenticated JSON endpoints are used. We deliberately
do NOT scrape LinkedIn/Indeed/Glassdoor: their terms forbid automated access
and doing so gets accounts banned.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Callable, Iterable

import requests

from jobhunter.models import Job, html_to_text
from jobhunter.profile import Profile

log = logging.getLogger(__name__)

USER_AGENT = "job-hunter/0.1 (personal job search tool)"
TIMEOUT = 30

# (url) -> parsed JSON. Swappable in tests.
Fetcher = Callable[[str], object]


def http_get_json(url: str) -> object:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def detect_ats(url: str) -> str:
    u = (url or "").lower()
    if "greenhouse.io" in u:
        return "greenhouse"
    if "lever.co" in u:
        return "lever"
    if "ashbyhq.com" in u:
        return "ashby"
    return ""


def _looks_remote(*texts: str) -> bool:
    return any(re.search(r"\bremote\b|anywhere|worldwide", t or "", re.I) for t in texts)


# --- Greenhouse --------------------------------------------------------------

def greenhouse(board: str, fetch: Fetcher = http_get_json) -> list[Job]:
    data = fetch(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true")
    jobs = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        url = j.get("absolute_url", "")
        jobs.append(Job(
            source="greenhouse",
            external_id=f"{board}/{j['id']}",
            company=j.get("company_name") or board.title(),
            title=j.get("title", ""),
            url=url,
            apply_url=url,
            location=loc,
            remote=_looks_remote(loc),
            description=html_to_text(j.get("content", "")),
            posted_at=j.get("first_published") or j.get("updated_at", ""),
            ats="greenhouse",
        ))
    return jobs


# --- Lever -------------------------------------------------------------------

def lever(company: str, fetch: Fetcher = http_get_json) -> list[Job]:
    data = fetch(f"https://api.lever.co/v0/postings/{company}?mode=json")
    jobs = []
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        loc = cats.get("location", "") or ", ".join(cats.get("allLocations") or [])
        parts = [j.get("descriptionPlain", "")]
        for lst in j.get("lists") or []:
            parts.append(lst.get("text", ""))
            parts.append(html_to_text(lst.get("content", "")))
        parts.append(j.get("additionalPlain", ""))
        salary = j.get("salaryRange") or {}
        created = j.get("createdAt")
        jobs.append(Job(
            source="lever",
            external_id=f"{company}/{j['id']}",
            company=company.title(),
            title=j.get("text", ""),
            url=j.get("hostedUrl", ""),
            apply_url=j.get("applyUrl") or (j.get("hostedUrl", "") + "/apply"),
            location=loc,
            remote=j.get("workplaceType") == "remote" or _looks_remote(loc),
            description="\n".join(p for p in parts if p),
            salary_min=salary.get("min"),
            salary_max=salary.get("max"),
            posted_at=(datetime.fromtimestamp(created / 1000, timezone.utc).isoformat()
                       if created else ""),
            ats="lever",
        ))
    return jobs


# --- Ashby -------------------------------------------------------------------

def ashby(org: str, fetch: Fetcher = http_get_json) -> list[Job]:
    data = fetch(f"https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true")
    jobs = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        loc = j.get("location", "")
        comp = j.get("compensation") or {}
        smin = smax = None
        for tier in comp.get("summaryComponents") or []:
            if tier.get("compensationType") == "Salary" and tier.get("currencyCode") == "USD":
                smin, smax = tier.get("minValue"), tier.get("maxValue")
        url = j.get("jobUrl", "")
        jobs.append(Job(
            source="ashby",
            external_id=f"{org}/{j['id']}",
            company=org.title(),
            title=j.get("title", ""),
            url=url,
            apply_url=j.get("applyUrl") or url,
            location=loc,
            remote=bool(j.get("isRemote")) or _looks_remote(loc),
            description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")),
            salary_min=int(smin) if smin else None,
            salary_max=int(smax) if smax else None,
            posted_at=j.get("publishedAt", ""),
            ats="ashby",
        ))
    return jobs


# --- Remotive (keyword search, remote only) ----------------------------------

def remotive(keyword: str, fetch: Fetcher = http_get_json) -> list[Job]:
    q = requests.utils.quote(keyword)
    data = fetch(f"https://remotive.com/api/remote-jobs?search={q}")
    jobs = []
    for j in data.get("jobs", []):
        url = j.get("url", "")
        jobs.append(Job(
            source="remotive",
            external_id=str(j["id"]),
            company=j.get("company_name", ""),
            title=j.get("title", ""),
            url=url,
            apply_url=url,
            location=j.get("candidate_required_location", "") or "Remote",
            remote=True,
            description=html_to_text(j.get("description", "")),
            posted_at=j.get("publication_date", ""),
            ats=detect_ats(url),
        ))
    return jobs


# --- RemoteOK ----------------------------------------------------------------

def remoteok(fetch: Fetcher = http_get_json) -> list[Job]:
    data = fetch("https://remoteok.com/api")
    jobs = []
    for j in data if isinstance(data, list) else []:
        if "id" not in j or "position" not in j:   # first element is a legal notice
            continue
        apply_url = j.get("apply_url") or j.get("url", "")
        jobs.append(Job(
            source="remoteok",
            external_id=str(j["id"]),
            company=j.get("company", ""),
            title=j.get("position", ""),
            url=j.get("url", ""),
            apply_url=apply_url,
            location=j.get("location", "") or "Remote",
            remote=True,
            description=html_to_text(j.get("description", "")) + "\n" + " ".join(j.get("tags") or []),
            salary_min=j.get("salary_min") or None,
            salary_max=j.get("salary_max") or None,
            posted_at=j.get("date", ""),
            ats=detect_ats(apply_url),
        ))
    return jobs


# --- checking board names ----------------------------------------------------

BOARD_FETCHERS: dict[str, Callable[..., list[Job]]] = {
    "greenhouse": greenhouse, "lever": lever, "ashby": ashby,
}


def probe_board(ats: str, slug: str, fetch: Fetcher | None = None) -> tuple[list[Job] | None, str]:
    """Try one board. Returns (jobs, "") if it exists, or (None, error)."""
    try:
        return BOARD_FETCHERS[ats](slug, fetch or http_get_json), ""
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "?"
        return None, f"HTTP {code}"
    except Exception as exc:  # noqa: BLE001
        return None, exc.__class__.__name__


# --- orchestration -----------------------------------------------------------

def fetch_all(profile: Profile, fetch: Fetcher | None = None) -> Iterable[Job]:
    """Yield jobs from every configured source; one broken board never stops the rest."""
    fetch = fetch or http_get_json
    tasks: list[tuple[str, Callable[[], list[Job]]]] = []
    s = profile.sources
    tasks += [(f"greenhouse:{b}", lambda b=b: greenhouse(b, fetch)) for b in s.greenhouse]
    tasks += [(f"lever:{c}", lambda c=c: lever(c, fetch)) for c in s.lever]
    tasks += [(f"ashby:{o}", lambda o=o: ashby(o, fetch)) for o in s.ashby]
    if s.remotive:
        kws = profile.search.keywords or profile.search.titles or [""]
        tasks += [(f"remotive:{k}", lambda k=k: remotive(k, fetch)) for k in kws]
    if s.remoteok:
        tasks.append(("remoteok", lambda: remoteok(fetch)))

    def run(task: Callable[[], list[Job]]) -> tuple[list[Job], Exception | None]:
        try:
            return task(), None
        except Exception as exc:  # noqa: BLE001 - keep going on any source failure
            return [], exc

    # Boards are fetched 8 at a time; results come back in the configured order.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=8) as pool:
        for (name, _), (found, exc) in zip(tasks, pool.map(run, [t for _, t in tasks])):
            if exc is None:
                log.info("%-30s %4d jobs", name, len(found))
                yield from found
            else:
                log.warning("%-30s FAILED: %s", name, exc)
