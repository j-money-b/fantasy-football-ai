#!/usr/bin/env python3
"""Emails the rendered weekly brief over SMTP.

Deliberately provider-agnostic: any SMTP host works (Gmail, Fastmail, iCloud,
Resend, Mailgun, SendGrid), so switching providers is a secrets change and not
a code change. Port 465 implies implicit TLS; anything else attempts STARTTLS.

Config comes entirely from the environment so no credential is ever written
to the repo:

    SMTP_HOST      smtp.gmail.com
    SMTP_PORT      465 (default 587)
    SMTP_USER      the account that authenticates
    SMTP_PASSWORD  app password / API key
    BRIEF_TO       recipient
    BRIEF_FROM     sender (defaults to SMTP_USER)

Exits non-zero on failure so the workflow's fallback path can run and the
absence of a brief is never silent (PRD R1)."""

import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path

REQUIRED = ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "BRIEF_TO")


def main(argv):
    if len(argv) < 3:
        print("usage: send_brief_email.py <brief.html> <subject.txt> [brief.md]", file=sys.stderr)
        return 2

    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if missing:
        print(f"ERROR: missing required environment: {', '.join(missing)}", file=sys.stderr)
        return 1

    html = Path(argv[1]).read_text(encoding="utf-8")
    subject = Path(argv[2]).read_text(encoding="utf-8").strip() or "Weekly fantasy brief"
    # A text/plain alternative is not optional: some clients and most
    # accessibility tooling read it instead of the HTML, and a brief that
    # arrives blank is indistinguishable from one that never arrived.
    plain = Path(argv[3]).read_text(encoding="utf-8") if len(argv) > 3 and Path(argv[3]).exists() else _strip(html)

    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    sender = os.environ.get("BRIEF_FROM") or user
    recipient = os.environ["BRIEF_TO"]

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(plain)
    message.add_alternative(html, subtype="html")

    context = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as smtp:
                smtp.login(user, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                smtp.starttls(context=context)
                smtp.login(user, password)
                smtp.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        print(f"ERROR: SMTP rejected the credentials ({exc.smtp_code}). For Gmail this usually means "
              f"an app password is required rather than the account password.", file=sys.stderr)
        return 1
    except (smtplib.SMTPException, OSError) as exc:
        print(f"ERROR: could not send the brief: {exc}", file=sys.stderr)
        return 1

    print(f"Brief emailed to {recipient} -- subject: {subject}")
    return 0


def _strip(html):
    """Last-resort plain-text fallback. The workflow passes the real markdown
    brief instead, which is why this only has to be readable, not pretty."""
    import re

    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</tr>|</div>|</p>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&middot;", "-").replace("&mdash;", "--")
    text = text.replace("&ndash;", "-").replace("&rarr;", "->").replace("&amp;", "&")
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]{2,}", " ", text)).strip()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
