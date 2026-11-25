"""M6 unit tests: SMTP outcome classification by protocol phase, draft rendering and
the draft hash binding."""

from __future__ import annotations

import socketserver
import threading
import uuid
from collections.abc import Iterator
from typing import ClassVar

import pytest

from app.actions.mail import Ambiguous, NotSent, Rejected, SmtpMailer, build_message
from app.actions.render import UnsupportedSource, draft_sha256, render_report, source_sha256


class _Script(socketserver.StreamRequestHandler):
    """A scripted SMTP peer. ``mode`` picks where it misbehaves."""

    mode = "ok"
    received: ClassVar[list[bytes]] = []

    def _reply(self, line: str) -> None:
        self.wfile.write(line.encode() + b"\r\n")
        self.wfile.flush()

    def handle(self) -> None:
        self._reply("220 test ESMTP")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            command = line.decode().strip().upper()
            if command.startswith(("EHLO", "HELO")):
                self._reply("250 test")
            elif command.startswith("MAIL"):
                self._reply("250 ok")
            elif command.startswith("RCPT"):
                self._reply("550 no such user" if self.mode == "reject_rcpt" else "250 ok")
            elif command == "DATA":
                self._reply("354 go ahead")
                body = b""
                while not body.endswith(b"\r\n.\r\n"):
                    chunk = self.rfile.readline()
                    if not chunk:
                        return
                    body += chunk
                type(self).received.append(body)
                if self.mode == "hang_after_data":
                    threading.Event().wait(3)  # accepted, but the reply never comes
                    return
                if self.mode == "drop_after_data":
                    return  # connection closed with no reply
                self._reply("554 rejected" if self.mode == "reject_data" else "250 queued")
            elif command == "QUIT":
                self._reply("221 bye")
                return
            else:
                self._reply("250 ok")


@pytest.fixture
def smtp_server() -> Iterator[tuple[str, int, type[_Script]]]:
    handler = type("Handler", (_Script,), {"received": []})
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield "127.0.0.1", server.server_address[1], handler
    server.shutdown()
    server.server_close()


def _message() -> object:
    return build_message(
        sender="Enterprise AI OS <no-reply@eaios.local>",
        recipient="to@niletech.example",
        subject="Verified report",
        body="Body\n.\nwith a lone dot line",
        message_id=f"<{uuid.uuid4()}@eaios.local>",
    )


def test_accepted(smtp_server: tuple[str, int, type[_Script]]) -> None:
    host, port, handler = smtp_server
    handler.mode = "ok"
    mailer = SmtpMailer(host, port, 2)
    mailer.send(_message(), "no-reply@eaios.local", "to@niletech.example")  # type: ignore[arg-type]
    (body,) = handler.received
    assert b"Message-ID: <" in body and b"X-EAIOS-Approved: human-approved" in body
    assert b"\r\n..\r\n" in body  # dot-stuffed lone dot line


def test_connection_refused_is_not_sent() -> None:
    with pytest.raises(NotSent):
        SmtpMailer("127.0.0.1", 9, 0.5).send(_message(), "a@b", "to@niletech.example")  # type: ignore[arg-type]


def test_recipient_rejection_before_data_is_permanent(
    smtp_server: tuple[str, int, type[_Script]],
) -> None:
    host, port, handler = smtp_server
    handler.mode = "reject_rcpt"
    with pytest.raises(Rejected):
        SmtpMailer(host, port, 2).send(_message(), "a@b", "to@niletech.example")  # type: ignore[arg-type]
    assert handler.received == []


def test_explicit_data_rejection_is_not_ambiguous(
    smtp_server: tuple[str, int, type[_Script]],
) -> None:
    host, port, handler = smtp_server
    handler.mode = "reject_data"
    with pytest.raises(Rejected):
        SmtpMailer(host, port, 2).send(_message(), "a@b", "to@niletech.example")  # type: ignore[arg-type]


@pytest.mark.parametrize("mode", ["hang_after_data", "drop_after_data"])
def test_no_reply_after_data_is_ambiguous(
    smtp_server: tuple[str, int, type[_Script]], mode: str
) -> None:
    host, port, handler = smtp_server
    handler.mode = mode
    with pytest.raises(Ambiguous):
        SmtpMailer(host, port, 0.5).send(_message(), "a@b", "to@niletech.example")  # type: ignore[arg-type]
    assert len(handler.received) == 1  # the server did get the message


RESULT = {
    "title": "Net sales, Q1 2026",
    "table": {
        "columns": [{"key": "net_sales", "label": "Net sales", "type": "money"}],
        "rows": [[{"column": "net_sales", "value": "1.00", "display": "USD 1.00"}]],
    },
    "narrative": [{"text": "Net sales were USD 1.00.", "citations": []}],
    "definition": {"date_basis": "order_date", "included_statuses": ["FULFILLED"]},
    "verification": {"label": "figures_verified", "inference_label": "FAKE INFERENCE"},
    "evidence": {"evidence_id": "e1", "result_sha256": "ab", "dataset_fingerprint": "d:1"},
}


def test_render_uses_only_the_published_result() -> None:
    subject, body = render_report(RESULT, "Finance Reader")
    assert subject == "Verified report: Net sales, Q1 2026"
    assert "USD 1.00" in body and "figures_verified" in body and "FAKE INFERENCE" in body
    assert "Finance Reader" in body
    with pytest.raises(UnsupportedSource):
        render_report({"answer": "documents result"}, "x")


def test_draft_hash_binds_every_field() -> None:
    base = {
        "tenant_id": uuid.uuid4(),
        "action_id": uuid.uuid4(),
        "version": 1,
        "recipient_user_id": uuid.uuid4(),
        "recipient_email": "to@niletech.example",
        "subject": "s",
        "body": "b",
        "source_sha": source_sha256(RESULT),
    }
    reference = draft_sha256(**base)  # type: ignore[arg-type]
    for key, value in (
        ("tenant_id", uuid.uuid4()),
        ("action_id", uuid.uuid4()),
        ("version", 2),
        ("recipient_user_id", uuid.uuid4()),
        ("recipient_email", "other@niletech.example"),
        ("subject", "S"),
        ("body", "b "),
        ("source_sha", "0" * 64),
    ):
        assert draft_sha256(**{**base, key: value}) != reference, key  # type: ignore[arg-type]
