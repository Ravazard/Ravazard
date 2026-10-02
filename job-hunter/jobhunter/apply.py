"""Fill out and submit application forms with a real browser (Playwright).

One generic, label-driven form filler handles Greenhouse, Lever and Ashby:
it reads every form control on the page, works out what each one is asking
(from its <label>, aria-label, placeholder or name), and answers it from:

    1. your profile     (name, email, phone, LinkedIn, GitHub, location, ...)
    2. `answers:`       (canned answers to screening questions in profile.yaml)
    3. Claude           (optional; only from facts in your profile)

Safety rails:
  * If any *required* question can't be answered, the job is marked
    needs_manual and nothing is submitted.
  * CAPTCHAs are never bypassed: the job is marked needs_manual.
  * With dry_run (the default), forms are filled and screenshotted but not submitted.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from jobhunter.models import Job, Status
from jobhunter.profile import Profile

log = logging.getLogger(__name__)

NAV_TIMEOUT_MS = 45_000


def launch_browser(pw, headless: bool = True):
    """Launch Chromium; JOBHUNTER_CHROMIUM can point at a specific binary."""
    exe = os.environ.get("JOBHUNTER_CHROMIUM") or None
    return pw.chromium.launch(headless=headless, executable_path=exe)

# Marks every visible form control with data-jh-idx and describes it.
DESCRIBE_FIELDS_JS = r"""
() => {
  const clean = s => (s || "").replace(/\s+/g, " ").replace(/\*/g, "").trim();
  const visible = el => {
    if (el.type === "file") return true;              // file inputs are usually hidden behind a button
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== "hidden" && st.display !== "none";
  };
  const labelFor = el => {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return clean(l.innerText);
    }
    const wrap = el.closest("label");
    if (wrap && clean(wrap.innerText)) return clean(wrap.innerText);
    if (el.getAttribute("aria-labelledby")) {
      const t = el.getAttribute("aria-labelledby").split(/\s+/)
        .map(id => document.getElementById(id)).filter(Boolean).map(n => n.innerText).join(" ");
      if (clean(t)) return clean(t);
    }
    if (el.getAttribute("aria-label")) return clean(el.getAttribute("aria-label"));
    // Nearest ancestor that contains a label/legend-ish element.
    let node = el.parentElement;
    for (let i = 0; node && i < 5; i++, node = node.parentElement) {
      const l = node.querySelector("label, legend, .application-label, [class*=label], [class*=question]");
      if (l && !l.contains(el) && clean(l.innerText)) return clean(l.innerText);
    }
    return clean(el.placeholder || el.name || el.id);
  };
  const groupQuestion = el => {
    const fs = el.closest("fieldset");
    if (fs) {
      const lg = fs.querySelector("legend");
      if (lg && clean(lg.innerText)) return clean(lg.innerText);
    }
    let node = el.parentElement;
    for (let i = 0; node && i < 6; i++, node = node.parentElement) {
      const inputs = node.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`);
      if (inputs.length > 1 || i > 2) {
        const l = node.querySelector("legend, .application-label, [class*=label], [class*=question], label:not(:has(input))");
        if (l && clean(l.innerText)) return clean(l.innerText);
      }
    }
    return clean(el.name);
  };

  // Nearest heading/legend before a field, e.g. "Education" or "Employment".
  const heads = [...document.querySelectorAll("h1, h2, h3, h4, legend")];
  const sectionOf = el => {
    let sec = "";
    for (const h of heads) if (h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) sec = clean(h.innerText);
    return sec;
  };

  const out = [];
  const seenGroups = new Set();
  let idx = 0;
  for (const el of document.querySelectorAll("input, textarea, select")) {
    const type = (el.type || el.tagName).toLowerCase();
    if (["hidden", "submit", "button", "reset", "image", "search"].includes(type)) continue;
    if (el.disabled || !visible(el)) continue;
    const required = el.required || el.getAttribute("aria-required") === "true" ||
      /\*/.test((el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`)?.innerText) || "");

    if ((type === "radio" || type === "checkbox") && el.name) {
      const group = [...document.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`)];
      if (type === "radio" || group.length > 1) {
        if (seenGroups.has(el.name)) continue;
        seenGroups.add(el.name);
        const options = group.map(g => {
          g.setAttribute("data-jh-idx", String(idx));
          return clean(labelFor(g) || g.value);
        });
        out.push({ idx: idx++, kind: type + "-group", name: el.name, id: el.id, label: groupQuestion(el),
                   required: group.some(g => g.required) || required, options, section: sectionOf(el) });
        continue;
      }
    }
    el.setAttribute("data-jh-idx", String(idx));
    const options = el.tagName === "SELECT"
      ? [...el.options].filter(o => o.value !== "").map(o => clean(o.text)) : [];
    const kind = el.tagName === "SELECT" ? "select"
      : el.getAttribute("role") === "combobox" ? "combobox"
      : el.tagName === "TEXTAREA" ? "textarea" : type;
    out.push({ idx: idx++, kind, name: el.name || "", id: el.id || "", label: labelFor(el),
               placeholder: el.placeholder || "", required, options, section: sectionOf(el) });
  }
  return out;
}
"""

CAPTCHA_SELECTORS = [
    "iframe[src*='recaptcha']:visible",
    "iframe[src*='hcaptcha']:visible",
    "iframe[src*='challenges.cloudflare.com']:visible",
    ".h-captcha:visible",
    ".g-recaptcha:visible",
]

SUCCESS_RE = re.compile(
    r"thank you for (applying|your application|your interest)|application (has been )?"
    r"(submitted|received)|we('ve| have) received your application|successfully submitted",
    re.I,
)

SUBMIT_SELECTORS = [
    "button[type=submit]:visible",
    "input[type=submit]:visible",
    "button:has-text('Submit application'):visible",
    "button:has-text('Submit Application'):visible",
    "button:has-text('Submit'):visible",
]


@dataclass
class Field:
    idx: int
    kind: str
    label: str
    name: str = ""
    id: str = ""
    placeholder: str = ""
    required: bool = False
    options: list[str] = field(default_factory=list)
    section: str = ""      # nearest heading above the field, e.g. "Education"

    @property
    def key(self) -> str:
        """Everything we know about the field, for matching standard profile fields."""
        return " ".join([self.label, self.name, self.id, self.placeholder]).lower()


@dataclass
class ApplyResult:
    status: Status
    note: str = ""
    screenshot: str = ""
    filled: dict[str, str] = field(default_factory=dict)


# (regex over field.key, profile attribute or callable) -- first match wins.
def _standard_fields(profile: Profile) -> list[tuple[str, str]]:
    p = profile.personal
    return [
        (r"first[\s_-]?name|given[\s_-]?name|preferred first", p.first_name),
        (r"last[\s_-]?name|family[\s_-]?name|surname", p.last_name),
        (r"\bfull[\s_-]?name\b|^name\b|\bname\b(?!.*(company|employer|school|reference))", p.full_name),
        (r"e-?mail", p.email),
        (r"phone|mobile", p.phone),
        (r"linkedin", p.linkedin),
        (r"github", p.github),
        (r"website|portfolio|personal (site|url)|\burls\[other\]", p.website or p.github),
        (r"current (company|employer)|most recent (company|employer)|^org\b|\borg\b", p.current_company),
        (r"\blocation\b|\bcity\b|where are you (based|located)|current address", p.location),
        (r"years of (professional |relevant )?experience", str(int(p.years_experience))),
    ]


_MONTHS = ["january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december"]


def _month_forms(answer: str) -> set[str]:
    """'July' -> {'july', 'jul', '7', '07'} so month dropdowns match however they're written."""
    a = answer.strip().lower()
    for i, m in enumerate(_MONTHS, 1):
        if a in (m, m[:3], str(i), f"{i:02d}"):
            return {m, m[:3], str(i), f"{i:02d}"}
    return set()


_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)")
_ATLEAST = re.compile(r"(\d+(?:\.\d+)?)\s*\+|(?:more than|above|over|greater than)\s*(\d+(?:\.\d+)?)")
_LESS = re.compile(r"(?:less than|under|below|<)\s*(\d+(?:\.\d+)?)")


def _years_option(years: float, options: list[str]) -> str | None:
    """Pick the closest bucket containing `years`: '1-3 years', '2+ years', 'Less than 1 year'..."""
    fits: list[tuple[float, int, str]] = []          # (lower bound, -position, option)
    for i, opt in enumerate(options):
        o = opt.lower()
        if m := _RANGE.search(o):
            lo, hi = float(m.group(1)), float(m.group(2))
            if lo <= years <= hi:
                fits.append((lo, -i, opt))
        elif m := _LESS.search(o):
            if years < float(m.group(1)):
                fits.append((-1.0, -i, opt))
        elif m := _ATLEAST.search(o):
            n = float(m.group(1) or m.group(2))
            if years >= n:
                fits.append((n, -i, opt))
        elif m := re.fullmatch(r"\s*(\d+)\s*(?:years?|yrs?)?\s*", o):   # plain "2 years" / "2"
            if int(m.group(1)) == int(years):
                fits.append((float(m.group(1)), -i, opt))
    return max(fits)[2] if fits else None


def pick_option(answer: str, options: list[str]) -> str | None:
    """Choose the option that best matches a free-text answer."""
    if not options:
        return None
    a = answer.strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", a):      # a number of years -> pick the bucket
        if (hit := _years_option(float(a), options)) is not None:
            return hit
    months = _month_forms(answer)
    if months:
        for opt in options:
            if opt.strip().lower() in months:
                return opt
    for opt in options:                       # exact
        if opt.strip().lower() == a:
            return opt
    for opt in options:                       # option starts with answer ("Yes, I am ...")
        if opt.strip().lower().startswith(a) and a:
            return opt
    for opt in options:                       # answer contained in option
        if a and a in opt.lower():
            return opt
    head = a.split(",")[0].strip()            # "Chennai, India" vs "Chennai, Tamil Nadu, India"
    if head and head != a:
        for opt in options:
            if opt.strip().lower().startswith(head):
                return opt
    return None


class Resolver:
    """Decides what to type into each field."""

    def __init__(self, profile: Profile, job: Job, use_llm: bool):
        self.profile = profile
        self.job = job
        self.use_llm = use_llm
        self._std = _standard_fields(profile)

    def resolve(self, f: Field) -> str | None:
        # Your education dates must never land in a job-history section that uses
        # the same labels ("Start date month"...). Leave those for you to fill.
        if re.search(r"employ|experience|work history|previous (job|role)", f.section, re.I) and \
                re.search(r"\b(start|end|graduation|from|to)\b", f.label, re.I):
            return None
        text_like = f.kind in {"text", "email", "tel", "url", "textarea", "combobox", "number"}
        if text_like:
            for pattern, value in self._std:
                if value and re.search(pattern, f.key):
                    return value
        if re.search(r"cover letter", f.key) and f.kind == "textarea":
            return self.cover_letter()

        canned = self.profile.canned_answer(f.label) if f.label else None
        if isinstance(canned, list):              # several acceptable answers, in order
            choices = [c for c in canned if c]
            if f.options:
                for c in choices:
                    if (hit := pick_option(c, f.options)) is not None:
                        return hit
                return None
            return choices[0] if choices else None
        if canned is not None and canned != "":
            if f.options:
                return pick_option(canned, f.options)
            return canned

        if f.kind == "checkbox" and f.required and re.search(
            r"agree|consent|acknowledge|privacy|terms|certify|confirm", f.key
        ):
            return "check"

        if self.use_llm and f.label and (f.required or f.kind in {"textarea", "select", "radio-group"}):
            from jobhunter import llm

            return llm.answer_question(f.label, f.options, self.job, self.profile)
        return None

    def alternatives(self, f: Field) -> list[str]:
        """Every acceptable answer for this field (when the profile gives a list)."""
        canned = self.profile.canned_answer(f.label) if f.label else None
        return [c for c in canned if c] if isinstance(canned, list) else []

    def cover_letter(self) -> str:
        if self.profile.apply.use_llm_for_cover_letter:
            from jobhunter import llm

            try:
                return llm.write_cover_letter(self.job, self.profile)
            except Exception as exc:  # noqa: BLE001
                log.warning("LLM cover letter failed, using template: %s", exc)
        return self.profile.cover_letter(self.job.company, self.job.title)


def _has_captcha(frame) -> bool:
    return any(frame.locator(sel).count() > 0 for sel in CAPTCHA_SELECTORS)


def _form_frame(page):
    """Greenhouse is often embedded in an iframe on the company's own site."""
    best, best_n = page.main_frame, -1
    for fr in page.frames:
        try:
            n = fr.locator("input:not([type=hidden]), textarea, select").count()
        except Exception:  # noqa: BLE001 - detached frames
            continue
        if n > best_n:
            best, best_n = fr, n
    return best


def _open_form(page, job: Job) -> None:
    page.goto(job.apply_url or job.url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    page.wait_for_timeout(1500)
    # Some pages show the posting first with an "Apply" button.
    frame = _form_frame(page)
    if frame.locator("input[type=email], input[type=file]").count() == 0:
        for sel in ["a:has-text('Apply for this job')", "button:has-text('Apply for this job')",
                    "a:has-text('Apply now')", "button:has-text('Apply now')",
                    "a:has-text('Apply')", "button:has-text('Apply')"]:
            btn = page.locator(sel).first
            if btn.count() and btn.is_visible():
                btn.click()
                page.wait_for_timeout(2000)
                break


def _menu_options(frame):
    """Options of the dropdown menu that is currently open (waits for slow/async lists)."""
    options = frame.locator("[role=option]")
    for _ in range(8):
        frame.page.wait_for_timeout(300)
        if options.count():
            break
    return options, [t.strip() for t in options.all_inner_texts()]


def _fill_combobox(frame, box, candidates: list[str]) -> str:
    """Dropdowns built as search boxes (Greenhouse questions, country, city, school...).

    1. Open the menu and pick from everything it lists, trying each acceptable answer
       (experience ranges like "1-3 Years" match a numeric answer such as "2.17").
    2. If nothing matched, type each answer as a search (long lists such as cities
       or schools only show results after typing) and pick the matching result.
    Never just takes the first option: typing "India" also lists "British Indian Ocean Territory".
    """
    box.click()
    options, texts = _menu_options(frame)
    for c in candidates:
        if texts and (hit := pick_option(c, texts)) is not None:
            options.nth(texts.index(hit)).click()
            return hit
    for c in candidates:
        if re.fullmatch(r"\d+(?:\.\d+)?", c.strip()):
            continue                                  # numbers only make sense against a full list
        box.fill(c.split(",")[0].strip())             # search "Chennai", then pick the full match
        options, texts = _menu_options(frame)
        if texts and (hit := pick_option(c, texts)) is not None:
            options.nth(texts.index(hit)).click()
            return hit
    box.press("Escape")
    raise LookupError(f"no option matching {candidates}")


def _fill(frame, f: Field, value: str, resume: Path | None, cover_file: Path | None,
          alts: list[str] | None = None) -> bool | str:
    loc = frame.locator(f'[data-jh-idx="{f.idx}"]')
    if f.kind == "file":
        path = cover_file if re.search(r"cover", f.key) else resume
        if not path:
            return False
        loc.first.set_input_files(str(path))
        return True
    if f.kind == "select":
        loc.first.select_option(label=value)
        return True
    if f.kind in ("radio-group", "checkbox-group"):
        i = f.options.index(value)
        loc.nth(i).check(force=True)
        return True
    if f.kind in ("checkbox", "radio"):
        loc.first.check(force=True)
        return True
    if f.kind == "combobox":
        return _fill_combobox(frame, loc.first, [value] + [a for a in (alts or []) if a != value])
    loc.first.fill(value)
    return True


def inspect_form(job: Job, profile: Profile, *, browser) -> list[tuple[Field, str | None]]:
    """Open the form and report each field with the answer we'd give. Fills nothing."""
    resolver = Resolver(profile, job, use_llm=False)
    context = browser.new_context(viewport={"width": 1280, "height": 1800})
    page = context.new_page()
    page.set_default_timeout(8_000)
    try:
        _open_form(page, job)
        frame = _form_frame(page)
        fields = [Field(**d) for d in frame.evaluate(DESCRIBE_FIELDS_JS)]
        out = []
        for f in fields:
            if f.kind == "file":
                out.append((f, "(resume / cover letter upload)"))
                continue
            value = resolver.resolve(f)
            if f.kind == "combobox":
                try:
                    box = frame.locator(f'[data-jh-idx="{f.idx}"]').first
                    box.click()
                    _, f.options = _menu_options(frame)
                    box.press("Escape")
                except Exception:  # noqa: BLE001 - diagnostics only
                    pass
                if value and f.options:
                    cands = [value] + [a for a in resolver.alternatives(f) if a != value]
                    hit = next((h for c in cands if (h := pick_option(c, f.options))), None)
                    value = hit or f"{cands} match none of the listed options (a search may still find it)"
            elif value and f.options:
                value = pick_option(value, f.options) or f"{value!r} matches no option"
            out.append((f, value))
        return out
    finally:
        context.close()


def apply_to_job(
    job: Job,
    profile: Profile,
    *,
    dry_run: bool = True,
    headless: bool = True,
    screenshot_dir: Path = Path("screenshots"),
    browser=None,
    human: Callable[[str, object], None] | None = None,
) -> ApplyResult:
    """Fill the application form for `job`, and submit it unless dry_run.

    `human`, if given, is called when the form needs a person (a CAPTCHA, or a
    required question we can't answer). It receives a message and the page, and
    returns once the person has finished the form and clicked Submit themselves.
    CAPTCHAs are never solved or bypassed by this code.
    """
    if job.ats not in {"greenhouse", "lever", "ashby"}:
        return ApplyResult(Status.NEEDS_MANUAL, f"unsupported application site ({job.apply_url})")

    resume = profile.resume_file
    if not resume or not resume.exists():
        return ApplyResult(Status.FAILED, f"resume not found at {resume}")

    from jobhunter.sources import company_from_slug

    if re.search(r"[-_]\d+$|^[a-z0-9-]+$", job.company):   # a raw board slug like "Brillio-2"
        job.company = company_from_slug(job.company)

    screenshot_dir.mkdir(parents=True, exist_ok=True)
    shot = screenshot_dir / f"{re.sub(r'[^A-Za-z0-9_-]+', '_', job.key)}.png"
    resolver = Resolver(profile, job, use_llm=profile.apply.use_llm_for_questions)

    own_pw = None
    if browser is None:
        from playwright.sync_api import sync_playwright

        own_pw = sync_playwright().start()
        browser = launch_browser(own_pw, headless)
    context = browser.new_context(viewport={"width": 1280, "height": 1800})
    page = context.new_page()
    page.set_default_timeout(8_000)   # fail fast on a stuck field instead of hanging 30s
    try:
        _open_form(page, job)
        frame = _form_frame(page)
        captcha = _has_captcha(frame)
        if captcha and human is None and not dry_run:
            page.screenshot(path=str(shot), full_page=True)
            return ApplyResult(Status.NEEDS_MANUAL,
                               "captcha on form: finish it with `apply --submit --show-browser`", str(shot))

        fields = [Field(**d) for d in frame.evaluate(DESCRIBE_FIELDS_JS)]
        if not any(f.kind == "email" or "email" in f.key for f in fields):
            page.screenshot(path=str(shot), full_page=True)
            return ApplyResult(Status.NEEDS_MANUAL, "couldn't find the application form", str(shot))

        with tempfile.TemporaryDirectory() as tmp:
            cover_file = None
            if any(f.kind == "file" and "cover" in f.key for f in fields):
                cover_file = Path(tmp) / "cover_letter.txt"
                cover_file.write_text(resolver.cover_letter())

            filled: dict[str, str] = {}
            missing: list[str] = []
            for f in fields:
                try:
                    if f.kind == "file":
                        # Cover-letter upload gets the letter; the first other upload gets the resume.
                        slot = "cover letter" if "cover" in f.key else "resume"
                        if slot not in filled and _fill(frame, f, "", resume, cover_file):
                            filled[slot] = "uploaded"
                        elif f.required:
                            missing.append(f.label or f.name)
                        continue

                    value = resolver.resolve(f)
                    if value is None or value == "":
                        if f.required:
                            missing.append(f.label or f.name)
                        continue
                    if f.options and f.kind != "combobox":
                        choice = pick_option(value, f.options)
                        if choice is None:
                            if f.required:
                                missing.append(f"{f.label} (no option matches '{value}')")
                            continue
                        value = choice
                    got = _fill(frame, f, value, resume, cover_file, resolver.alternatives(f))
                    shown = " ".join((got if isinstance(got, str) else value).split())
                    filled[f.label or f.name] = shown if len(shown) < 80 else shown[:77] + "..."
                except Exception as exc:  # noqa: BLE001
                    log.debug("could not fill %s: %s", f.label, exc)
                    if f.required:
                        missing.append(f"{f.label} (error: {exc.__class__.__name__})")

            page.screenshot(path=str(shot), full_page=True)
            if dry_run:
                note = "form filled, not submitted (dry run)"
                if captcha:
                    note += "; has a CAPTCHA, so you'll finish it yourself with --submit --show-browser"
                if missing:
                    note += "; you'll need to answer: " + "; ".join(missing[:6])
                return ApplyResult(Status.DRY_RUN, note, str(shot), filled)
            if (captcha or missing) and human is None:
                why = "captcha on form" if captcha else "unanswered required: " + "; ".join(missing[:6])
                return ApplyResult(Status.NEEDS_MANUAL, why, str(shot), filled)

            if captcha or missing:
                # Hand over to the person at the keyboard; they submit, not us.
                todo = []
                if missing:
                    todo.append("answer: " + "; ".join(missing[:6]))
                if captcha:
                    todo.append("complete the CAPTCHA")
                human("In the browser window, " + " and ".join(todo)
                      + ", then click Submit yourself.", page)
                try:
                    body = ""
                    for _ in range(15):                  # the confirmation page can take a while
                        page.wait_for_timeout(1000)
                        body = " ".join(fr.locator("body").inner_text()
                                        for fr in page.frames if fr.locator("body").count())
                        if SUCCESS_RE.search(body):
                            break
                    page.screenshot(path=str(shot), full_page=True)
                except Exception:  # noqa: BLE001 - the person closed the browser window
                    why = "captcha on form" if captcha else "unanswered required: " + "; ".join(missing[:6])
                    return ApplyResult(Status.NEEDS_MANUAL,
                                       f"{why} (browser was closed before Submit; run again)", str(shot), filled)
                if SUCCESS_RE.search(body):
                    return ApplyResult(Status.APPLIED, "submitted by you after the form was filled", str(shot), filled)
                return ApplyResult(Status.NEEDS_MANUAL,
                                   "no confirmation seen after your submit; check your email", str(shot), filled)

            submit = None
            for sel in SUBMIT_SELECTORS:
                cand = frame.locator(sel).last
                if cand.count():
                    submit = cand
                    break
            if submit is None:
                return ApplyResult(Status.NEEDS_MANUAL, "no submit button found", str(shot), filled)

            submit.click()
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:  # noqa: BLE001 - SPAs never go idle; that's fine
                pass
            page.wait_for_timeout(2000)
            page.screenshot(path=str(shot), full_page=True)

            body = " ".join(fr.locator("body").inner_text() for fr in page.frames if fr.locator("body").count())
            if SUCCESS_RE.search(body):
                return ApplyResult(Status.APPLIED, "submitted", str(shot), filled)
            if _has_captcha(_form_frame(page)):
                return ApplyResult(Status.NEEDS_MANUAL, "captcha appeared on submit", str(shot), filled)
            # We clicked submit but can't confirm it. Never auto-retry: that could double-apply.
            return ApplyResult(Status.NEEDS_MANUAL,
                               "clicked submit but couldn't confirm; check your email", str(shot), filled)
    except Exception as exc:  # noqa: BLE001
        try:
            page.screenshot(path=str(shot), full_page=True)
        except Exception:  # noqa: BLE001
            pass
        return ApplyResult(Status.FAILED, f"{exc.__class__.__name__}: {exc}"[:300], str(shot))
    finally:
        context.close()
        if own_pw:
            browser.close()
            own_pw.stop()
