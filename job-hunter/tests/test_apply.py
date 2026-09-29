"""Drive the real form filler against a local copy of a Greenhouse-style form."""

import shutil
from pathlib import Path

import pytest

from jobhunter.apply import apply_to_job, launch_browser, pick_option
from jobhunter.models import Job, Status
from jobhunter.profile import load_profile

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "fixtures"

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as pw:
        try:
            b = launch_browser(pw)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no chromium: {exc}")
        yield b
        b.close()


@pytest.fixture
def setup(tmp_path):
    for name in ("form.html", "thanks.html"):
        shutil.copy(FIX / name, tmp_path / name)
    (tmp_path / "resume.pdf").write_bytes(b"%PDF-1.4\n% fake resume\n")
    profile = load_profile(ROOT / "profile.dba.example.yaml")
    profile.personal.resume_path = str(tmp_path / "resume.pdf")
    job = Job(source="greenhouse", external_id="acme/101", company="Acme",
              title="Database Administrator", url="x",
              apply_url=(tmp_path / "form.html").as_uri(), ats="greenhouse")
    return tmp_path, profile, job


def test_pick_option():
    assert pick_option("yes", ["Yes", "No"]) == "Yes"
    assert pick_option("No", ["Yes, I will", "No, I won't"]) == "No, I won't"
    assert pick_option("maybe", ["Yes", "No"]) is None


def test_dry_run_fills_but_does_not_submit(setup, browser):
    tmp, profile, job = setup
    res = apply_to_job(job, profile, dry_run=True, browser=browser, screenshot_dir=tmp / "shots")
    assert res.status is Status.DRY_RUN, res.note
    assert res.filled["First Name"] == "Your"
    assert res.filled["Email"] == "you@example.com"
    assert res.filled["resume"] == "uploaded"
    assert res.filled["How did you hear about this job?"] == "Company careers page"
    assert res.filled["Will you now or in the future require visa sponsorship?"] == "No"
    assert res.filled["I agree to the privacy policy"] == "check"
    assert "Hi Acme team" in res.filled["Cover Letter"]
    assert Path(res.screenshot).exists()


def test_submit_detects_confirmation(setup, browser):
    tmp, profile, job = setup
    res = apply_to_job(job, profile, dry_run=False, browser=browser, screenshot_dir=tmp / "shots")
    assert res.status is Status.APPLIED, res.note


def test_unknown_required_question_blocks_submit(setup, browser):
    tmp, profile, job = setup
    html = (tmp / "form.html").read_text().replace(
        "<!--EXTRA-->",
        '<div><label for="q9">Describe your largest database migration *</label>'
        '<textarea id="q9" name="q9" required></textarea></div>',
    )
    (tmp / "form.html").write_text(html)
    res = apply_to_job(job, profile, dry_run=False, browser=browser, screenshot_dir=tmp / "shots")
    assert res.status is Status.NEEDS_MANUAL
    assert "largest database migration" in res.note


def test_captcha_is_never_bypassed(setup, browser):
    tmp, profile, job = setup
    html = (tmp / "form.html").read_text().replace(
        "<!--EXTRA-->", '<div class="g-recaptcha" style="width:300px;height:80px"></div>'
    )
    (tmp / "form.html").write_text(html)
    res = apply_to_job(job, profile, dry_run=False, browser=browser, screenshot_dir=tmp / "shots")
    assert res.status is Status.NEEDS_MANUAL and "captcha" in res.note


def test_unsupported_site(setup):
    _, profile, job = setup
    job.ats = ""
    assert apply_to_job(job, profile).status is Status.NEEDS_MANUAL
