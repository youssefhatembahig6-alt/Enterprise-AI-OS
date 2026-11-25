"""Turn a published, verified report into an email draft, and hash drafts.

The body is built only from the run's stored, verified ``result`` (typed table cells,
checked narrative, verification label and evidence reference). Nothing from retrieved
documents or model output outside that published result is used, and recipients are
never taken from content.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID


class UnsupportedSource(Exception):
    """The run is not a published verified report this version can turn into email."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def source_sha256(result: dict[str, Any]) -> str:
    """Hash of the exact published result the draft was built from."""

    return hashlib.sha256(canonical_json(result)).hexdigest()


def draft_sha256(
    *,
    tenant_id: UUID,
    action_id: UUID,
    version: int,
    recipient_user_id: UUID,
    recipient_email: str,
    subject: str,
    body: str,
    source_sha: str,
) -> str:
    """Hash binding everything a send depends on. An approval names this value, so a
    change to any field (even by a direct database edit) invalidates it."""

    return hashlib.sha256(
        canonical_json(
            {
                "tenant_id": str(tenant_id),
                "action_id": str(action_id),
                "version": version,
                "recipient_user_id": str(recipient_user_id),
                "recipient_email": recipient_email,
                "subject": subject,
                "body": body,
                "source_sha256": source_sha,
            }
        )
    ).hexdigest()


def render_report(result: dict[str, Any], sender_name: str) -> tuple[str, str]:
    """Subject and plain-text body for a verified sales report result."""

    try:
        title = str(result["title"])
        columns = result["table"]["columns"]
        (cells,) = result["table"]["rows"]
        definition = result["definition"]
        verification = result["verification"]
        evidence = result["evidence"]
    except (KeyError, TypeError, ValueError) as exc:
        raise UnsupportedSource("not a verified report result") from exc

    labels = {c["key"]: c["label"] for c in columns}
    width = max(len(label) for label in labels.values())
    lines = [f"{title}", "", "Verified figures:"]
    for cell in cells:
        lines.append(
            f"  {labels.get(cell['column'], cell['column']).ljust(width)}  {cell['display']}"
        )
    narrative = [s["text"] for s in result.get("narrative") or [] if s.get("text")]
    if narrative:
        lines += ["", "Summary:"]
        lines += [f"  {sentence}" for sentence in narrative]
    lines += [
        "",
        "Definition:",
        f"  Date basis: {definition.get('date_basis')}",
        f"  Included statuses: {', '.join(definition.get('included_statuses') or [])}",
        f"  Currency: {definition.get('currency')}",
        f"  Tax treatment: {definition.get('tax_treatment')}",
        "",
        f"Verification: {verification.get('label')}",
    ]
    if verification.get("inference_label"):
        lines.append(f"  {verification['inference_label']}")
    lines += [
        f"Evidence: {evidence.get('evidence_id')} (result sha256 {evidence.get('result_sha256')})",
        f"Dataset: {evidence.get('dataset_fingerprint')}",
        "",
        f"Prepared by Enterprise AI OS for {sender_name}. Sent only after their approval.",
    ]
    subject = f"Verified report: {title}"[:200].replace("\r", " ").replace("\n", " ")
    return subject, "\n".join(lines)
