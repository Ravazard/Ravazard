import json
from pathlib import Path

import yaml

from jobhunter import digest, sources
from jobhunter.cli import main

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "fixtures"


def fake_fetch(url: str):
    name = "greenhouse" if "greenhouse" in url else "lever" if "lever" in url else "ashby"
    return json.loads((FIX / f"{name}.json").read_text())


class FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        if password != "app-password-123":
            import smtplib
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
        self.user = user

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


def setup(tmp_path, monkeypatch):
    data = yaml.safe_load((ROOT / "profile.dba.example.yaml").read_text())
    data["sources"] = {"greenhouse": ["acme"], "lever": ["globex"], "ashby": ["initech"]}
    data["notify"]["email_to"] = "me@mail.test"
    prof = tmp_path / "profile.yaml"
    prof.write_text(yaml.safe_dump(data))
    monkeypatch.setattr(sources, "http_get_json", fake_fetch)
    monkeypatch.setattr(digest.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("JOBHUNTER_SMTP_PASSWORD", " app-pass word-123 ")
    FakeSMTP.sent = []
    return ["--profile", str(prof), "--db", str(tmp_path / "jobs.db")]


def test_digest_emails_new_matches_once(tmp_path, monkeypatch, capsys):
    base = setup(tmp_path, monkeypatch)
    (tmp_path / "search.log").write_text("INFO greenhouse:acme  4 jobs\n")
    assert main(base + ["search"]) == 0

    assert main(base + ["digest", "--log", str(tmp_path / "search.log")]) == 0
    assert len(FakeSMTP.sent) == 1
    msg = FakeSMTP.sent[0]
    assert msg["To"] == "me@mail.test" and "2 new matches" in msg["Subject"]
    text = msg.get_body(("plain",)).get_content()
    html = msg.get_body(("html",)).get_content()
    # Full JD, link and the exact approve command are in the email.
    assert "Manage PostgreSQL and MySQL clusters" in text
    assert "python -m jobhunter approve greenhouse:acme/101" in text
    assert "https://job-boards.greenhouse.io/acme/jobs/101" in html
    assert "greenhouse:acme  4 jobs" in text            # log tail included
    assert "Account Executive" not in text               # rejected jobs aren't mailed

    # Second run the same night: nothing new, so no repeat of the same jobs.
    assert main(base + ["digest", "--skip-empty"]) == 0
    assert len(FakeSMTP.sent) == 1
    assert main(base + ["digest"]) == 0
    assert "no new matches" in FakeSMTP.sent[1]["Subject"]


def test_digest_preview_sends_nothing(tmp_path, monkeypatch):
    base = setup(tmp_path, monkeypatch)
    main(base + ["search"])
    out = tmp_path / "digest.html"
    assert main(base + ["digest", "--preview", str(out)]) == 0
    assert FakeSMTP.sent == [] and "Database Administrator" in out.read_text()


def test_missing_password_is_reported(tmp_path, monkeypatch, capsys):
    base = setup(tmp_path, monkeypatch)
    monkeypatch.delenv("JOBHUNTER_SMTP_PASSWORD")
    monkeypatch.setattr(digest, "PASSWORD_FILE", tmp_path / "nope")
    monkeypatch.setattr(digest.sys, "platform", "linux")
    main(base + ["search"])
    assert main(base + ["digest"]) == 1
    assert "security add-generic-password" in capsys.readouterr().err


def test_rejected_password_gives_clear_advice(tmp_path, monkeypatch, capsys):
    base = setup(tmp_path, monkeypatch)
    monkeypatch.setenv("JOBHUNTER_SMTP_PASSWORD", "my-normal-gmail-password")
    main(base + ["search"])
    assert main(base + ["digest"]) == 1
    err = capsys.readouterr().err
    assert "app password" in err and "add-generic-password -U" in err


def test_normalize_app_password():
    import pytest

    assert digest.normalize_app_password("abcd efgh ijkl mnop") == "abcdefghijklmnop"
    assert digest.normalize_app_password(" ABCD EFGH\tIJKL MNOP\n") == "abcdefghijklmnop"
    with pytest.raises(digest.DigestError, match="17 characters"):
        digest.normalize_app_password("MyNormalPass2024!")   # a regular password
    with pytest.raises(digest.DigestError, match=r"position 3 is 'digit'.*1 typed instead of l"):
        digest.normalize_app_password("ab1d efgh ijkl mnop")


def test_set_email_password_saves_and_tests(tmp_path, monkeypatch, capsys):
    base = setup(tmp_path, monkeypatch)
    monkeypatch.delenv("JOBHUNTER_SMTP_PASSWORD")
    monkeypatch.setattr(digest.sys, "platform", "linux")
    monkeypatch.setattr(digest, "PASSWORD_FILE", tmp_path / "cfg" / "smtp_password")
    FakeSMTP.expected = "abcdefghijklmnop"
    monkeypatch.setattr(FakeSMTP, "login", lambda self, u, p: None if p == FakeSMTP.expected
                        else (_ for _ in ()).throw(__import__("smtplib").SMTPAuthenticationError(535, b"no")))

    monkeypatch.setattr("getpass.getpass", lambda prompt="": "MyNormalPass2024!")
    assert main(base + ["set-email-password"]) == 1
    assert "17 characters" in capsys.readouterr().err
    assert not (tmp_path / "cfg" / "smtp_password").exists()

    monkeypatch.setattr("getpass.getpass", lambda prompt="": "abcd efgh ijkl mnop")
    assert main(base + ["set-email-password"]) == 0
    assert "Login works" in capsys.readouterr().out
    f = tmp_path / "cfg" / "smtp_password"
    assert f.read_text() == "abcdefghijklmnop" and oct(f.stat().st_mode & 0o777) == "0o600"
