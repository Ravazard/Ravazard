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


def _captcha_form(tmp):
    html = (tmp / "form.html").read_text().replace(
        "<!--EXTRA-->", '<div class="g-recaptcha" style="width:300px;height:80px"></div>')
    (tmp / "form.html").write_text(html)


def test_captcha_dry_run_still_fills_the_form(setup, browser):
    tmp, profile, job = setup
    _captcha_form(tmp)
    res = apply_to_job(job, profile, dry_run=True, browser=browser, screenshot_dir=tmp / "shots")
    assert res.status is Status.DRY_RUN and "CAPTCHA" in res.note
    assert res.filled["Email"] == "you@example.com"


def test_captcha_handed_to_a_person_who_submits(setup, browser):
    tmp, profile, job = setup
    _captcha_form(tmp)
    seen = {}

    def person(message, page):
        seen["message"] = message
        # The tool has filled everything; the person does the CAPTCHA and clicks Submit.
        assert page.locator("#email").input_value() == "you@example.com"
        page.click("button[type=submit]")
        page.wait_for_load_state()

    res = apply_to_job(job, profile, dry_run=False, browser=browser,
                       screenshot_dir=tmp / "shots", human=person)
    assert "complete the CAPTCHA" in seen["message"]
    assert res.status is Status.APPLIED and "submitted by you" in res.note


def test_person_gives_up_is_not_counted_as_applied(setup, browser):
    tmp, profile, job = setup
    _captcha_form(tmp)
    res = apply_to_job(job, profile, dry_run=False, browser=browser,
                       screenshot_dir=tmp / "shots", human=lambda message, page: None)
    assert res.status is Status.NEEDS_MANUAL and "no confirmation" in res.note


def test_person_answers_unknown_question(setup, browser):
    tmp, profile, job = setup
    html = (tmp / "form.html").read_text().replace(
        "<!--EXTRA-->",
        '<div><label for="q9">Describe your largest database migration *</label>'
        '<textarea id="q9" name="q9" required></textarea></div>')
    (tmp / "form.html").write_text(html)

    def person(message, page):
        assert "largest database migration" in message
        page.fill("#q9", "Moved 2 TB from MySQL 5.7 to 8.0 with zero data loss.")
        page.click("button[type=submit]")
        page.wait_for_load_state()

    res = apply_to_job(job, profile, dry_run=False, browser=browser,
                       screenshot_dir=tmp / "shots", human=person)
    assert res.status is Status.APPLIED


COMBOS = """
<div><label for="country">Country *</label><input id="country" role="combobox" required><ul id="lb1" role="listbox"></ul></div>
<div><label for="city">Location (City) *</label><input id="city" role="combobox" required><ul id="lb2" role="listbox"></ul></div>
<script>
function wire(inp, list, all) {
  inp.addEventListener('input', () => {
    list.innerHTML = '';
    const q = inp.value.toLowerCase();
    setTimeout(() => all.filter(x => x.toLowerCase().includes(q)).forEach(x => {
      const li = document.createElement('li'); li.setAttribute('role', 'option'); li.textContent = x;
      li.onclick = () => { inp.value = x; list.innerHTML = ''; };
      list.appendChild(li);
    }), 300);   // results arrive late, like a real search
  });
}
wire(document.getElementById('country'), document.getElementById('lb1'), ['British Indian Ocean Territory', 'India', 'Indonesia']);
wire(document.getElementById('city'), document.getElementById('lb2'), ['Chengdu, China', 'Chennai, Tamil Nadu, India']);
</script>
"""


def test_search_dropdowns_pick_the_exact_match(setup, browser):
    tmp, profile, job = setup
    (tmp / "form.html").write_text((tmp / "form.html").read_text().replace("<!--EXTRA-->", COMBOS))
    profile.personal.location = "Chennai, India"
    profile.answers["country"] = "India"
    res = apply_to_job(job, profile, dry_run=True, browser=browser, screenshot_dir=tmp / "shots")
    assert res.status is Status.DRY_RUN, res.note
    assert res.filled["Country"] == "India"                        # not "British Indian Ocean Territory"
    assert res.filled["Location (City)"] == "Chennai, India"


def test_dropdown_without_a_match_is_left_for_the_person(setup, browser):
    tmp, profile, job = setup
    (tmp / "form.html").write_text((tmp / "form.html").read_text().replace("<!--EXTRA-->", COMBOS))
    profile.personal.location = "Coimbatore, India"               # not in the list
    profile.answers["country"] = "India"
    res = apply_to_job(job, profile, dry_run=False, browser=browser, screenshot_dir=tmp / "shots")
    assert res.status is Status.NEEDS_MANUAL and "Location (City)" in res.note


def test_closing_the_browser_during_handover_is_not_a_failure(setup, browser):
    tmp, profile, job = setup
    _captcha_form(tmp)
    res = apply_to_job(job, profile, dry_run=False, browser=browser, screenshot_dir=tmp / "shots",
                       human=lambda message, page: page.close())
    assert res.status is Status.NEEDS_MANUAL
    assert res.note.startswith("captcha") and "closed" in res.note   # so the next run picks it up again


EDUCATION = """
<div><label for="school">School *</label><input id="school" name="school" required></div>
<div><label for="sm">Start date month *</label>
  <select id="sm" name="sm" required><option value="">Month</option><option>Jun</option><option>Jul</option><option>Aug</option></select></div>
<div><label for="sy">Start date year *</label><input id="sy" name="sy" required></div>
<div><label for="em">End date month</label>
  <select id="em" name="em"><option value="">Month</option><option>06</option><option>07</option></select></div>
<div><label for="ey">End date year</label><input id="ey" name="ey"></div>
"""


def test_education_dates_filled(setup, browser):
    tmp, profile, job = setup
    (tmp / "form.html").write_text((tmp / "form.html").read_text().replace("<!--EXTRA-->", EDUCATION))
    profile.answers.update({
        "school": "Amrita Vishwa Vidyapeetham",
        "start date month": "July", "start date year": "2020",
        "end date month": "July", "end date year": "2024",
    })
    res = apply_to_job(job, profile, dry_run=True, browser=browser, screenshot_dir=tmp / "shots")
    assert res.status is Status.DRY_RUN, res.note
    f = res.filled
    assert f["School"] == "Amrita Vishwa Vidyapeetham"
    assert f["Start date month"] == "Jul" and f["Start date year"] == "2020"     # "July" -> "Jul"
    assert f["End date month"] == "07" and f["End date year"] == "2024"          # "July" -> "07"


def test_month_matching():
    assert pick_option("July", ["Jun", "Jul", "Aug"]) == "Jul"
    assert pick_option("July", ["06", "07"]) == "07"
    assert pick_option("July", ["January", "July"]) == "July"
    assert pick_option("Mayo", ["May", "June"]) is None


def test_education_dates_stay_out_of_employment_section(setup, browser):
    tmp, profile, job = setup
    employment = """
<h3>Education</h3>""" + EDUCATION + """
<h3>Employment</h3>
<div><label for="wsm">Start date month</label>
  <select id="wsm" name="wsm"><option value="">Month</option><option>Jul</option><option>Aug</option></select></div>
<div><label for="wsy">Start date year</label><input id="wsy" name="wsy"></div>
"""
    (tmp / "form.html").write_text((tmp / "form.html").read_text().replace("<!--EXTRA-->", employment))
    profile.answers.update({"school": "Amrita Vishwa Vidyapeetham",
                            "start date month": "July", "start date year": "2020"})
    seen = {}

    def person(message, page):        # look at the real field values before submitting
        seen.update(edu_year=page.locator("#sy").input_value(), job_year=page.locator("#wsy").input_value(),
                    edu_month=page.locator("#sm").input_value(), job_month=page.locator("#wsm").input_value())
        page.click("button[type=submit]")
        page.wait_for_load_state()

    # A CAPTCHA forces the hand-over, so the test can inspect the filled form.
    (tmp / "form.html").write_text((tmp / "form.html").read_text().replace(
        "</form>", '<div class="g-recaptcha" style="width:300px;height:80px"></div></form>'))
    res = apply_to_job(job, profile, dry_run=False, browser=browser, screenshot_dir=tmp / "shots", human=person)
    assert res.status is Status.APPLIED, res.note
    assert seen["edu_year"] == "2020" and seen["edu_month"] == "Jul"
    assert seen["job_year"] == "" and seen["job_month"] == ""      # employment section left alone


def test_inspect_lists_fields_without_filling(setup, browser):
    from jobhunter.apply import inspect_form
    tmp, profile, job = setup
    (tmp / "form.html").write_text((tmp / "form.html").read_text().replace(
        "<!--EXTRA-->", "<h3>Education</h3>" + EDUCATION))
    profile.answers.update({"school": "Amrita Vishwa Vidyapeetham", "start date month": "July"})
    rows = {f.label: (f, v) for f, v in inspect_form(job, profile, browser=browser)}
    assert rows["School"][1] == "Amrita Vishwa Vidyapeetham"
    assert rows["School"][0].section == "Education"
    assert rows["Start date month"][1] == "Jul"
    assert rows["Start date year"][1] is None            # no answer configured in this test
    assert rows["Email"][1] == "you@example.com"


def test_years_buckets_and_answer_lists():
    cases = [
        ("2.17", ["0-1 years", "1-3 years", "3-5 years", "5+ years"], "1-3 years"),
        ("2.17", ["Less than 1 year", "1 - 2 Years", "2 - 3 Years", "3+ Years"], "2 - 3 Years"),
        ("2.17", ["Fresher", "1+ years", "2+ years", "5+ years"], "2+ years"),
        ("2.17", ["0 years", "1 year", "2 years", "3 years"], "2 years"),
        ("2.17", ["Less than 3 years", "2-3 years"], "2-3 years"),
        ("0.5", ["Less than 1 year", "1-3 years"], "Less than 1 year"),
        ("2.17", ["Yes", "No"], None),
    ]
    for answer, options, want in cases:
        assert pick_option(answer, options) == want, (answer, options)


SIGMOID_LIKE = """
<div><label for="desig">Current Designation *</label><input id="desig" name="desig" required></div>
<div><label for="tot">Total Professional Experience *</label>
  <select id="tot" name="tot" required><option value="">Select</option><option>0-1 Years</option>
  <option>1-3 Years</option><option>3-5 Years</option></select></div>
<div><label for="rel">Relevant Professional Experience *</label><input id="rel" name="rel" required></div>
<div><label for="src">How did you come to know about Sigmoid? *</label>
  <select id="src" name="src" required><option value="">Select</option><option>Naukri</option>
  <option>Job Board</option><option>LinkedIn</option><option>Referral</option></select></div>
<div><label for="word">What is the one word that comes to your mind when you think of Sigmoid? *</label>
  <input id="word" name="word" required></div>
"""


def test_screening_questions_from_answer_lists(setup, browser):
    tmp, profile, job = setup
    (tmp / "form.html").write_text((tmp / "form.html").read_text().replace("<!--EXTRA-->", SIGMOID_LIKE))
    profile.answers.update({
        "current designation": "MySQL Database Administrator",
        "total professional experience": ["2 years 2 months", "2.17"],
        "relevant professional experience": ["2 years 2 months", "2.17"],
        "how did you come to know": ["Job Board", "LinkedIn"],
    })
    res = apply_to_job(job, profile, dry_run=True, browser=browser, screenshot_dir=tmp / "shots")
    f = res.filled
    assert f["Current Designation"] == "MySQL Database Administrator"
    assert f["Total Professional Experience"] == "1-3 Years"           # bucket from 2.17
    assert f["Relevant Professional Experience"] == "2 years 2 months"  # free text: first answer
    assert f["How did you come to know about Sigmoid?"] == "Job Board"
    # The opinion question has no answer: it's left for the person.
    assert "one word" in res.note
