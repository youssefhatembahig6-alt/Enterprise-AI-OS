"""SMTP delivery to Mailpit and Mailpit lookups for reconciliation.

The SMTP exchange is driven step by step so a failure can be classified by *where* it
happened:

* before ``DATA`` was sent (connect, EHLO, MAIL, RCPT): the server cannot have accepted
  the message -> ``NotSent`` (safe to retry) or ``Rejected`` (5xx, permanent);
* after the message bytes were handed to the server but before a ``250`` reply was read
  (timeout, disconnect): the server may have accepted it -> ``Ambiguous``.

Mailpit's HTTP API is then the ground truth used to resolve ambiguity. That is a
property of this development mail server; a real SMTP relay offers no such lookup,
so this does not amount to an end-to-end exactly-once guarantee.
"""

from __future__ import annotations

import contextlib
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from typing import Any, Protocol

import httpx


class NotSent(Exception):
    """Definitely not accepted; may be retried with the same Message-ID."""


class Rejected(Exception):
    """Permanently refused by the server (5xx before acceptance)."""


class Ambiguous(Exception):
    """The server may have accepted the message; reconciliation is required."""


class MailpitUnavailable(Exception):
    """Mailpit's API could not answer; the outcome stays uncertain."""


@dataclass(frozen=True, slots=True)
class Accepted:
    reply: str


class Mailer(Protocol):
    def send(self, message: EmailMessage, sender: str, recipient: str) -> Accepted: ...


class MailLookup(Protocol):
    def find(self, message_id: str, recipient: str) -> str | None: ...


class SmtpMailer:
    def __init__(self, host: str, port: int, timeout: float) -> None:
        self._host, self._port, self._timeout = host, port, timeout

    def send(self, message: EmailMessage, sender: str, recipient: str) -> Accepted:
        try:
            smtp = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
        except (OSError, smtplib.SMTPException) as exc:
            raise NotSent(f"connect: {type(exc).__name__}") from exc
        data_started = False
        try:
            code, _ = smtp.ehlo()
            if not 200 <= code < 300:
                raise NotSent(f"ehlo {code}")
            code, _ = smtp.mail(sender)
            if code != 250:
                raise (Rejected if code >= 500 else NotSent)(f"mail {code}")
            code, _ = smtp.rcpt(recipient)
            if code not in (250, 251):
                raise (Rejected if code >= 500 else NotSent)(f"rcpt {code}")
            payload = message.as_bytes(policy=SMTP_POLICY)
            data_started = True
            try:
                code, reply = smtp.data(payload)
            except smtplib.SMTPDataError as exc:
                # An explicit non-354 reply to the DATA command: nothing was accepted.
                raise (Rejected if exc.smtp_code >= 500 else NotSent)(
                    f"data {exc.smtp_code}"
                ) from exc
            # smtplib returns (not raises) the reply to the message body. Only 250 means
            # the server took responsibility for it; an explicit 4xx/5xx means it did not.
            if code != 250:
                raise (Rejected if code >= 500 else NotSent)(f"data reply {code}")
            return Accepted(
                reply.decode(errors="replace") if isinstance(reply, bytes) else str(reply)
            )
        except (NotSent, Rejected):
            raise
        except (TimeoutError, smtplib.SMTPServerDisconnected, OSError) as exc:
            if data_started:
                raise Ambiguous(f"{type(exc).__name__} after DATA") from exc
            raise NotSent(f"{type(exc).__name__} before DATA") from exc
        except smtplib.SMTPException as exc:
            if data_started:
                raise Ambiguous(type(exc).__name__) from exc
            raise NotSent(type(exc).__name__) from exc
        finally:
            with contextlib.suppress(Exception):  # the outcome is already decided
                smtp.quit()


class MailpitLookup:
    """Find a message in Mailpit by its Message-ID (and check the recipient)."""

    def __init__(self, api_url: str, timeout: float = 5.0) -> None:
        self._api = api_url.rstrip("/")
        self._timeout = timeout

    def find(self, message_id: str, recipient: str) -> str | None:
        bare = message_id.strip().strip("<>")
        try:
            response = httpx.get(
                f"{self._api}/api/v1/search",
                params={"query": f"message-id:{bare}", "limit": 10},
                timeout=self._timeout,
            )
            response.raise_for_status()
            body: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MailpitUnavailable(type(exc).__name__) from exc
        for message in body.get("messages") or []:
            if str(message.get("MessageID", "")).strip("<>") != bare:
                continue
            addresses = {str(a.get("Address", "")).lower() for a in message.get("To") or []}
            if recipient.lower() in addresses:
                return str(message["ID"])
        return None


def build_message(
    *, sender: str, recipient: str, subject: str, body: str, message_id: str
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message["Auto-Submitted"] = "auto-generated"
    message["X-EAIOS-Approved"] = "human-approved"
    message.set_content(body)
    return message
