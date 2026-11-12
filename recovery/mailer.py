"""Outbound email. Only ever talks to a local catcher (Mailpit), never a real mail server."""
from __future__ import annotations

import smtplib
from email.message import EmailMessage

from . import config

LOCAL_HOSTS = {"localhost", "127.0.0.1", "mailpit", "::1"}


class Mailer:
    def __init__(self, host: str | None = None, port: int | None = None):
        self.host = host or config.SMTP_HOST
        self.port = port or config.SMTP_PORT
        if self.host not in LOCAL_HOSTS:
            # Safety: this project must never email real people.
            raise RuntimeError(f"SMTP_HOST={self.host} is not local. Only Mailpit is allowed.")

    def send(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = config.MAIL_FROM, to, subject
        msg.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=10) as smtp:
            smtp.send_message(msg)
