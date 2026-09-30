"""Nightly email: new matches with their full job descriptions, so you can decide what to apply to.

The SMTP password (for Gmail, an "app password") is never stored in profile.yaml.
It is looked up, in order, from:
    1. the JOBHUNTER_SMTP_PASSWORD environment variable
    2. the macOS Keychain item "jobhunter-smtp"
    3. the file ~/.config/jobhunter/smtp_password (chmod 600)
"""

from __future__ import annotations

import html
import os
import smtplib
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

from jobhunter.models import Job, Status
from jobhunter.profile import Profile
from jobhunter.store import Store

KEYCHAIN_SERVICE = "jobhunter-smtp"
PASSWORD_FILE = Path("~/.config/jobhunter/smtp_password").expanduser()


class DigestError(RuntimeError):
    pass


def smtp_password(user: str) -> str:
    # Gmail shows app passwords as "abcd efgh ijkl mnop"; the spaces aren't part of it.
    return "".join(_raw_password(user).split())


def _raw_password(user: str) -> str:
    if pw := os.environ.get("JOBHUNTER_SMTP_PASSWORD"):
        return pw.strip()
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-a", user, "-s", KEYCHAIN_SERVICE, "-w"],
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    if PASSWORD_FILE.exists():
        return PASSWORD_FILE.read_text().strip()
    raise DigestError(
        "No SMTP password found. Save your Gmail app password with:\n"
        f"  security add-generic-password -a {user} -s {KEYCHAIN_SERVICE} -T /usr/bin/security -w"
    )


def normalize_app_password(raw: str) -> str:
    """Strip every kind of space; raise if what's left isn't a 16-letter Gmail app password."""
    pw = "".join(raw.split()).replace(" ", "")
    if len(pw) != 16:
        raise DigestError(
            f"That's {len(pw)} characters. A Gmail app password is exactly 16 letters "
            "(shown as 'abcd efgh ijkl mnop'). Your normal Gmail password won't work. "
            "Create one at https://myaccount.google.com/apppasswords"
        )
    odd = [(i + 1, c) for i, c in enumerate(pw) if not ("a" <= c.lower() <= "z")]
    if odd:
        where = ", ".join(f"position {i} is '{'digit' if c.isdigit() else 'symbol'}'" for i, c in odd)
        raise DigestError(
            f"16 characters, but {where}. App passwords are letters a-z only; "
            "check for 1 typed instead of l, or 0 instead of o. Copy-paste it rather than typing."
        )
    return pw.lower()


def save_password(user: str, pw: str) -> str:
    """Store in the macOS Keychain (replacing any old one), else in a chmod-600 file."""
    if sys.platform == "darwin":
        subprocess.run(["security", "delete-generic-password", "-a", user, "-s", KEYCHAIN_SERVICE],
                       capture_output=True)
        out = subprocess.run(["security", "add-generic-password", "-U", "-a", user, "-s", KEYCHAIN_SERVICE,
                              "-T", "/usr/bin/security", "-w", pw], capture_output=True, text=True)
        if out.returncode == 0:
            return "macOS Keychain"
    PASSWORD_FILE.parent.mkdir(parents=True, exist_ok=True)
    PASSWORD_FILE.write_text(pw)
    PASSWORD_FILE.chmod(0o600)
    return str(PASSWORD_FILE)


def check_login(profile: Profile) -> None:
    n = profile.notify
    user = n.smtp_user or n.email_to
    with smtplib.SMTP(n.smtp_host, n.smtp_port, timeout=60) as smtp:
        smtp.starttls()
        smtp.login(user, smtp_password(user))


def _approve_cmd(job: Job) -> str:
    return f"python -m jobhunter approve {job.key}"


def build_digest(store: Store, profile: Profile, log_path: Path | None = None,
                 hours: int = 24) -> tuple[str, str, str, list[str]]:
    """Returns (subject, plain_text, html_body, keys_of_jobs_included)."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    matches = store.not_emailed(Status.MATCHED, Status.APPROVED)
    applied = store.changed_since(since, Status.APPLIED)
    manual = store.changed_since(since, Status.NEEDS_MANUAL)
    checked = store.seen_since(since)
    limit = profile.notify.max_description_chars
    today = datetime.now().strftime("%d %b %Y")

    subject = (f"Job Hunter: {len(matches)} new match{'es' if len(matches) != 1 else ''} ({today})"
               if matches else f"Job Hunter: no new matches ({today})")

    # ---- plain text ----------------------------------------------------------
    t = [f"Job Hunter digest for {today}",
         f"New postings checked in the last {hours}h: {checked}",
         f"New matches: {len(matches)}   Applied: {len(applied)}   Need you: {len(manual)}", ""]
    for i, j in enumerate(matches, 1):
        t += [f"=== {i}. {j.title} @ {j.company} ===",
              f"Where: {'Remote' if j.remote else j.location}    Score: {j.score:.0f}",
              f"Link: {j.url}",
              "Why: " + "; ".join(j.reasons),
              f"To apply, run: {_approve_cmd(j)}", "",
              j.description[:limit] + (" ..." if len(j.description) > limit else ""), ""]
    if manual:
        t += ["--- Need you (captcha or unanswered question) ---"]
        t += [f"* {j.title} @ {j.company}: {j.url}" for j in manual] + [""]
    if applied:
        t += ["--- Applied ---"] + [f"* {j.title} @ {j.company}" for j in applied] + [""]
    log_tail = ""
    if log_path and log_path.exists():
        log_tail = "\n".join(log_path.read_text(errors="replace").splitlines()[-60:])
        t += ["--- Search log (last 60 lines) ---", log_tail]
    text = "\n".join(t)

    # ---- html ----------------------------------------------------------------
    e = html.escape
    h = [f"<h2 style='margin:0'>Job Hunter &middot; {e(today)}</h2>",
         f"<p style='color:#555'>Checked {checked} new postings in the last {hours}h &middot; "
         f"<b>{len(matches)}</b> new matches &middot; {len(applied)} applied &middot; "
         f"{len(manual)} need you</p>"]
    if not matches:
        h.append("<p>No new matches today. Nothing to decide.</p>")
    for i, j in enumerate(matches, 1):
        desc = e(j.description[:limit]).replace("\n", "<br>")
        more = " &hellip;" if len(j.description) > limit else ""
        h.append(
            "<div style='border:1px solid #ddd;border-radius:8px;padding:14px;margin:14px 0'>"
            f"<h3 style='margin:0 0 4px'>{i}. <a href='{e(j.url)}'>{e(j.title)}</a></h3>"
            f"<div><b>{e(j.company)}</b> &middot; {e('Remote' if j.remote else j.location)} "
            f"&middot; score <b>{j.score:.0f}</b></div>"
            f"<div style='color:#555;font-size:13px;margin:6px 0'>{e('; '.join(j.reasons))}</div>"
            "<div style='background:#f4f4f4;padding:8px;border-radius:6px;font-family:monospace;"
            f"font-size:13px'>To apply, run: {e(_approve_cmd(j))}</div>"
            f"<details open><summary>Job description</summary><p style='font-size:14px'>{desc}{more}</p>"
            "</details></div>")
    if manual:
        h.append("<h3>Need you</h3><ul>" + "".join(
            f"<li><a href='{e(j.url)}'>{e(j.title)}</a> @ {e(j.company)}</li>" for j in manual) + "</ul>")
    if applied:
        h.append("<h3>Applied</h3><ul>" + "".join(
            f"<li>{e(j.title)} @ {e(j.company)}</li>" for j in applied) + "</ul>")
    if log_tail:
        h.append("<h3>Search log</h3><pre style='font-size:12px;background:#f4f4f4;padding:8px'>"
                 f"{e(log_tail)}</pre>")
    body = "<div style='font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:760px'>" + "".join(h) + "</div>"
    return subject, text, body, [j.key for j in matches]


def send_email(profile: Profile, subject: str, text: str, html_body: str) -> None:
    n = profile.notify
    if not n.email_to:
        raise DigestError("Set notify.email_to in profile.yaml")
    user = n.smtp_user or n.email_to
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, n.email_to
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP(n.smtp_host, n.smtp_port, timeout=60) as smtp:
        smtp.starttls()
        try:
            smtp.login(user, smtp_password(user))
        except smtplib.SMTPAuthenticationError as exc:
            raise DigestError(
                f"Gmail rejected the login for {user} ({exc.smtp_code}). Use a 16-letter app password "
                "from https://myaccount.google.com/apppasswords, not your normal Gmail password. "
                f"Re-save it with:\n  security add-generic-password -U -a {user} -s {KEYCHAIN_SERVICE} "
                "-T /usr/bin/security -w"
            ) from exc
        smtp.send_message(msg)
