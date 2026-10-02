"""Command-line interface.

    python -m jobhunter init                 # create profile.yaml
    python -m jobhunter search [--llm]       # fetch + score new jobs
    python -m jobhunter list [--status X]    # see what matched
    python -m jobhunter show KEY
    python -m jobhunter approve KEY... | --top N
    python -m jobhunter skip KEY...
    python -m jobhunter apply [--submit]     # fill (and optionally submit) approved jobs
    python -m jobhunter run [--submit]       # search + apply in one go (for cron)
    python -m jobhunter rescore              # re-score after editing your profile
    python -m jobhunter stats
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from jobhunter.matcher import score_job
from jobhunter.models import Job, Status
from jobhunter.profile import Profile, load_profile
from jobhunter.store import FINAL, Store

log = logging.getLogger("jobhunter")
HERE = Path(__file__).resolve().parent.parent


# --- helpers -----------------------------------------------------------------

def _status_after_score(score: float, excluded: bool, profile: Profile) -> Status:
    if excluded or score < profile.apply.min_score:
        return Status.REJECTED
    return Status.APPROVED if profile.apply.auto_approve else Status.MATCHED


def _fmt_row(job: Job) -> str:
    where = "remote" if job.remote else job.location
    return f"{job.score:5.1f}  {job.status.value:<12} {job.key:<38} {job.company[:18]:<18} {job.title[:48]:<48} {where[:24]}"


def _print_jobs(jobs: list[Job], why: bool = False) -> None:
    if not jobs:
        print("(no jobs)")
        return
    print(f"{'score':>5}  {'status':<12} {'key':<38} {'company':<18} {'title':<48} where")
    for j in jobs:
        print(_fmt_row(j))
        if why and j.reasons:
            print(f"{'':>7}why: {'; '.join(j.reasons)[:150]}")


def _llm_rerank(store: Store, profile: Profile, jobs: list[Job]) -> None:
    from jobhunter import llm

    for job in jobs:
        try:
            fit = llm.assess_fit(job, profile)
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM scoring failed for %s: %s", job.key, exc)
            continue
        combined = round((job.score + fit.score) / 2, 1)
        reasons = job.reasons + [f"claude: {fit.score}/100 - {fit.summary}"]
        if fit.missing_skills:
            reasons.append("missing: " + ", ".join(fit.missing_skills[:5]))
        status = _status_after_score(combined, False, profile)
        store.set_score(job.key, combined, reasons, status)
        print(f"  {job.key}: rules {job.score:.0f} + claude {fit.score} -> {combined:.0f}")


# --- commands ----------------------------------------------------------------

def cmd_init(args) -> int:
    dest = Path(args.profile)
    if dest.exists():
        print(f"{dest} already exists; not overwriting.")
        return 1
    shutil.copy(HERE / "profile.example.yaml", dest)
    print(f"Created {dest}. Edit it with your details, skills, and target companies,")
    print("put your resume PDF next to it, then run:  python -m jobhunter search")
    return 0


def cmd_search(args, profile: Profile, store: Store) -> int:
    from jobhunter.sources import fetch_all

    new = kept = 0
    fresh: list[Job] = []
    for job in fetch_all(profile):
        if not store.upsert(job):
            continue
        new += 1
        m = score_job(job, profile)
        status = _status_after_score(m.score, m.excluded, profile)
        store.set_score(job.key, m.score, m.reasons, status)
        if status is not Status.REJECTED:
            kept += 1
            job.score, job.reasons, job.status = m.score, m.reasons, status
            fresh.append(job)
    print(f"{new} new jobs, {kept} good matches (score >= {profile.apply.min_score}).")

    if args.llm and fresh:
        print(f"Asking Claude to double-check {len(fresh)} matches...")
        _llm_rerank(store, profile, fresh)

    _print_jobs(store.by_status(Status.MATCHED, Status.APPROVED, limit=25))
    return 0


def cmd_rescore(args, profile: Profile, store: Store) -> int:
    jobs = store.by_status(Status.NEW, Status.REJECTED, Status.MATCHED, Status.APPROVED)
    for job in jobs:
        m = score_job(job, profile)
        status = _status_after_score(m.score, m.excluded, profile)
        if job.status is Status.APPROVED and status is Status.MATCHED:
            status = Status.APPROVED        # don't undo your manual approvals
        store.set_score(job.key, m.score, m.reasons, status)
    print(f"Re-scored {len(jobs)} jobs.")
    return 0


def cmd_list(args, profile: Profile, store: Store) -> int:
    statuses = [Status(s) for s in args.status] if args.status else [Status.MATCHED, Status.APPROVED]
    jobs = store.by_status(*statuses)
    if args.title:
        words = [w.lower() for w in args.title]
        jobs = [j for j in jobs if any(w in j.title.lower() for w in words)]
    _print_jobs(jobs[: args.limit], why=args.why)
    return 0


def cmd_show(args, profile: Profile, store: Store) -> int:
    job = store.get(args.key)
    if not job:
        print(f"No job {args.key}")
        return 1
    print(f"{job.title} @ {job.company}\n{job.url}\nlocation: {job.location}"
          f"{' (remote)' if job.remote else ''}\nstatus: {job.status.value}   score: {job.score}")
    if note := store.note(job.key):
        print(f"note: {note}")
    print("why:\n  - " + "\n  - ".join(job.reasons))
    print("\n" + job.description[: args.chars])
    return 0


def cmd_approve(args, profile: Profile, store: Store) -> int:
    keys = list(args.keys)
    if args.top:
        keys += [j.key for j in store.by_status(Status.MATCHED, limit=args.top)]
    for key in keys:
        if not store.get(key):
            print(f"  ? {key} (not found)")
            continue
        store.set_status(key, Status.APPROVED)
        print(f"  approved {key}")
    return 0


def cmd_skip(args, profile: Profile, store: Store) -> int:
    for key in args.keys:
        store.set_status(key, Status.SKIPPED)
        print(f"  skipped {key}")
    return 0


def cmd_apply(args, profile: Profile, store: Store) -> int:
    from jobhunter.apply import apply_to_job, launch_browser

    if problems := profile.problems():
        print("Fix these in your profile before applying:\n  - " + "\n  - ".join(problems))
        return 1
    submit = args.submit or not profile.apply.dry_run
    limit = min(args.limit or profile.apply.max_per_run, profile.apply.max_per_run)
    queue = store.by_status(Status.APPROVED, Status.DRY_RUN if submit else Status.APPROVED, limit=limit)

    # Watching the browser while submitting? Then you can finish CAPTCHAs and
    # unanswered questions yourself, so jobs that stopped for those come back.
    human = None
    if submit and args.show_browser and sys.stdin.isatty():
        def human(message: str, page) -> None:
            print(f"\n   >>> {message}")
            print("   >>> Don't type commands here and don't close the browser yet.")
            input("   >>> After clicking Submit (or to skip this job), press Enter here... ")
        handover = [j for j in store.by_status(Status.NEEDS_MANUAL)
                    if store.note(j.key).startswith(("captcha", "unanswered required"))]
        queue = (queue + handover)[:limit]
    if not queue:
        print("Nothing approved to apply to. Use `list` then `approve KEY` (or `approve --top N`).")
        return 0
    mode = "SUBMITTING" if submit else "DRY RUN (forms filled, nothing submitted)"
    print(f"{mode}: {len(queue)} job(s)\n")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = launch_browser(pw, headless=profile.apply.headless and not args.show_browser)
        for job in queue:
            if job.status in FINAL:
                continue
            print(f"-> {job.company}: {job.title}")
            res = apply_to_job(job, profile, dry_run=not submit, browser=browser,
                               screenshot_dir=Path(args.screenshots), human=human)
            store.set_status(job.key, res.status, res.note)
            print(f"   {res.status.value}: {res.note}")
            if res.filled:
                for q, a in list(res.filled.items())[:12]:
                    print(f"     {q[:50]:<50} = {a}")
            if res.screenshot:
                print(f"   screenshot: {res.screenshot}")
        browser.close()

    print()
    print({k: v for k, v in store.counts().items()})
    return 0


def cmd_inspect(args, profile: Profile, store: Store) -> int:
    from playwright.sync_api import sync_playwright

    from jobhunter.apply import inspect_form, launch_browser

    job = store.get(args.key)
    if not job:
        print(f"No job {args.key}")
        return 1
    print(f"Fields on the form for {job.title} @ {job.company} (nothing is filled or sent):\n")
    with sync_playwright() as pw:
        browser = launch_browser(pw, headless=not args.show_browser)
        rows = inspect_form(job, profile, browser=browser)
        browser.close()
    for f, value in rows:
        req = "*" if f.required else " "
        opts = f"  options: {', '.join(f.options[:6])}{' ...' if len(f.options) > 6 else ''}" if f.options else ""
        shown = " ".join(str(value).split()) if value else "NOT ANSWERED"
        print(f"{req} [{f.kind:<14}] {f.label[:55]:<55} section: {f.section[:20]:<20} -> "
              f"{shown[:60] + '...' if len(shown) > 60 else shown}")
        if f.name or f.id:
            print(f"      name={f.name!r} id={f.id!r}{opts}")
    unanswered = [f.label for f, v in rows if f.required and not v]
    print(f"\n{len(rows)} fields, {len(unanswered)} required ones without an answer"
          + (": " + "; ".join(unanswered) if unanswered else "."))
    return 0


def cmd_run(args, profile: Profile, store: Store) -> int:
    cmd_search(args, profile, store)
    return cmd_apply(args, profile, store)


def _replace_sources_block(text: str, sources: dict[str, list[str]], remotive: bool, remoteok: bool) -> str:
    """Swap the `sources:` section of profile.yaml, leaving everything else (and comments) alone."""
    import re
    from datetime import date

    def flow(xs: list[str]) -> str:
        return "[" + ", ".join(f'"{x}"' for x in xs) + "]"

    new = (
        f"sources:   # written by `check-boards` on {date.today().isoformat()}\n"
        f"  greenhouse: {flow(sources['greenhouse'])}\n"
        f"  lever: {flow(sources['lever'])}\n"
        f"  ashby: {flow(sources['ashby'])}\n"
        f"  remotive: {str(remotive).lower()}\n"
        f"  remoteok: {str(remoteok).lower()}\n"
    )
    # The block runs from "sources:" to the next top-level key; drop comments
    # that sit directly above that key so they stay with it.
    pattern = re.compile(r"^sources:.*\n(?:(?:[ \t].*|[ \t]*)\n)*", re.M)
    if not pattern.search(text):
        return text.rstrip("\n") + "\n\n" + new
    return pattern.sub(lambda _: new + "\n", text, count=1)


def cmd_check_boards(args, profile: Profile, store: Store) -> int:
    from concurrent.futures import ThreadPoolExecutor

    import yaml

    from jobhunter.matcher import location_matches
    from jobhunter.sources import probe_board

    candidates: dict[str, list[str]] = {"greenhouse": [], "lever": [], "ashby": []}
    for ats in candidates:                       # what's already in your profile
        candidates[ats] += getattr(profile.sources, ats)
    if args.file:
        data = yaml.safe_load(Path(args.file).read_text()) or {}
        for ats in candidates:
            candidates[ats] += [str(s) for s in data.get(ats) or []]
    pairs = [(ats, slug) for ats, slugs in candidates.items() for slug in dict.fromkeys(slugs)]
    print(f"Checking {len(pairs)} company boards...\n")

    places = profile.search.locations or ["India"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda p: (p, *probe_board(*p)), pairs))

    working: dict[str, list[str]] = {"greenhouse": [], "lever": [], "ashby": []}
    rows = []
    for (ats, slug), jobs, err in results:
        if jobs:                                 # an empty board can't be told apart from a wrong name
            local = sum(1 for j in jobs if location_matches(j.location, places))
            working[ats].append(slug)
            rows.append((local, len(jobs), f"{ats}:{slug}"))
        else:
            rows.append((-1, 0, f"{ats}:{slug}  ({err or 'no open jobs'})"))

    rows.sort(key=lambda r: (-r[0], -r[1]))
    for local, total, name in rows:
        if local >= 0:
            print(f"  OK    {name:<50} {total:5d} jobs, {local:4d} in {'/'.join(places)}")
    bad = [name for local, _, name in rows if local < 0]
    if bad:
        print(f"\n  {len(bad)} not found or empty (skipped): " + ", ".join(n.split()[0] for n in bad[:40])
              + (" ..." if len(bad) > 40 else ""))

    n = sum(len(v) for v in working.values())
    print(f"\n{n} working boards.")
    if not args.write:
        print("Run again with --write to put them in profile.yaml.")
        return 0
    path = Path(args.profile)
    path.write_text(_replace_sources_block(path.read_text(), working,
                                           profile.sources.remotive, profile.sources.remoteok))
    print(f"Updated the sources: section of {path}. Now run:  python -m jobhunter search")
    return 0


def cmd_digest(args, profile: Profile, store: Store) -> int:
    from jobhunter.digest import build_digest, send_email

    log = Path(args.log) if args.log else None
    subject, text, body, keys = build_digest(store, profile, log)
    if args.preview:
        out = Path(args.preview)
        out.write_text(body)
        print(f"{subject}\nPreview written to {out} (nothing sent).")
        return 0
    if args.skip_empty and not keys:
        print("No new matches; not sending.")
        return 0
    try:
        send_email(profile, subject, text, body)
    except Exception as exc:  # noqa: BLE001 - report any SMTP failure plainly
        print(f"Couldn't send the digest: {exc}", file=sys.stderr)
        return 1
    store.mark_emailed(keys)
    print(f"Sent '{subject}' to {profile.notify.email_to}")
    return 0


def cmd_set_email_password(args, profile: Profile, store: Store) -> int:
    import getpass
    import smtplib

    from jobhunter.digest import DigestError, normalize_app_password, save_password, check_login

    user = profile.notify.smtp_user or profile.notify.email_to
    if not user:
        print("Set notify.email_to in profile.yaml first.", file=sys.stderr)
        return 1
    print(f"Paste the 16-letter Gmail app password for {user}.")
    print("(Nothing shows while you paste; that's normal. Then press Return.)")
    try:
        pw = normalize_app_password(getpass.getpass("App password: "))
    except DigestError as exc:
        print(exc, file=sys.stderr)
        return 1
    where = save_password(user, pw)
    print(f"Saved to {where}. Testing the login with Gmail...")
    try:
        check_login(profile)
    except smtplib.SMTPAuthenticationError as exc:
        print(f"Gmail rejected it ({exc.smtp_code}). Create a NEW app password and run this again.",
              file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Couldn't reach Gmail: {exc}", file=sys.stderr)
        return 1
    print("Login works. Send yourself a digest with:  python -m jobhunter digest")
    return 0


def cmd_skills_report(args, profile: Profile, store: Store) -> int:
    from jobhunter.report import database_jobs, skill_counts

    jobs = database_jobs(store.by_status(*Status), profile, args.max_years)
    if not jobs:
        print("No database job postings collected yet. Run `search` for a few days first.")
        return 0
    print(f"Skills mentioned in {len(jobs)} database job postings"
          + (f" asking for at most {args.max_years} years" if args.max_years is not None else "")
          + " (all locations):\n")
    print(f"{'skill':<36} {'postings':>8}  {'share':>5}  on your profile?")
    for r in skill_counts(jobs, profile):
        if r.count == 0:
            continue
        bar = "#" * round(r.share * 20)
        print(f"{r.skill:<36} {r.count:>8}  {r.share:>5.0%}  {'yes' if r.yours else '-- gap':<8} {bar}")
    if len(jobs) < 30:
        print(f"\nOnly {len(jobs)} postings so far; treat this as a rough signal until more collect.")
    return 0


def cmd_stats(args, profile: Profile, store: Store) -> int:
    counts = store.counts()
    for s in Status:
        print(f"{s.value:<13} {counts.get(s.value, 0)}")
    print(f"\napplied today: {store.applied_today()}")
    return 0


# --- entry point -------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jobhunter", description="Find matching jobs and apply automatically.")
    p.add_argument("--profile", default="profile.yaml", help="path to your profile (default: profile.yaml)")
    p.add_argument("--db", default="data/jobs.db", help="SQLite database path")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create profile.yaml from the example")

    sp = sub.add_parser("search", help="fetch and score new jobs")
    sp.add_argument("--llm", action="store_true", help="have Claude re-rank new matches")

    sub.add_parser("rescore", help="re-score stored jobs after editing your profile")

    sp = sub.add_parser("list", help="list jobs")
    sp.add_argument("--status", nargs="*", choices=[s.value for s in Status])
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--title", nargs="+", help="only titles containing any of these words")
    sp.add_argument("--why", action="store_true", help="show the scoring reasons")

    sp = sub.add_parser("show", help="show one job in detail")
    sp.add_argument("key")
    sp.add_argument("--chars", type=int, default=2500, help="how much of the description to print")

    sp = sub.add_parser("approve", help="approve jobs for applying")
    sp.add_argument("keys", nargs="*")
    sp.add_argument("--top", type=int, help="approve the N best-scoring matches")

    sp = sub.add_parser("skip", help="never apply to these jobs")
    sp.add_argument("keys", nargs="+")

    sp = sub.add_parser("inspect", help="list a job's form fields and the answers we'd give (fills nothing)")
    sp.add_argument("key")
    sp.add_argument("--show-browser", action="store_true")

    for name in ("apply", "run"):
        sp = sub.add_parser(name, help="fill/submit applications" if name == "apply" else "search, then apply")
        sp.add_argument("--submit", action="store_true",
                        help="actually submit (overrides dry_run: true in profile.yaml)")
        sp.add_argument("--limit", type=int, help="max applications this run")
        sp.add_argument("--show-browser", action="store_true", help="watch the browser work")
        sp.add_argument("--screenshots", default="screenshots")
        if name == "run":
            sp.add_argument("--llm", action="store_true")

    sub.add_parser("stats", help="counts by status")

    sp = sub.add_parser("digest", help="email new matches (with full job descriptions) to you")
    sp.add_argument("--log", default="search.log", help="include the tail of this log file")
    sp.add_argument("--preview", metavar="FILE.html", help="write the email to a file instead of sending")
    sp.add_argument("--skip-empty", action="store_true", help="don't send when there are no new matches")

    sp = sub.add_parser("skills-report", help="which skills database job postings ask for")
    sp.add_argument("--max-years", type=int, default=None,
                    help="only postings asking for at most this many years (e.g. 4)")

    sub.add_parser("set-email-password", help="save and test the Gmail app password for digest")

    sp = sub.add_parser("check-boards", help="test company board names and keep the working ones")
    sp.add_argument("--file", default=str(HERE / "companies" / "india.yaml"),
                    help="YAML of candidate boards (default: companies/india.yaml)")
    sp.add_argument("--write", action="store_true", help="save the working boards into profile.yaml")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-7s %(message)s")
    if args.cmd == "init":
        return cmd_init(args)
    try:
        profile = load_profile(args.profile)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    store = Store(args.db)
    try:
        return globals()[f"cmd_{args.cmd.replace('-', '_')}"](args, profile, store)
    finally:
        store.close()
