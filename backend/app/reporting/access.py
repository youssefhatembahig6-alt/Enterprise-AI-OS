"""Current-state authorization for report datasets (reference policy, dataset.query)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Connection

from app.repositories.identity import load_subject
from app.repositories.resources import load_resource_meta
from app.security.policy import Action, Decision, Reason, evaluate


def authorize_dataset(conn: Connection, user_id: UUID, resource_id: UUID | None) -> Decision:
    """Evaluate ``dataset.query`` for the user's *current* PostgreSQL state.

    Called at run start, before query execution, when persisting evidence, inside the
    publication transaction, and on every evidence view or event replay.
    """

    if resource_id is None:
        return Decision.deny(Reason.NOT_FOUND)
    subject = load_subject(conn, user_id)
    resource = load_resource_meta(conn, resource_id)
    if subject is None or resource is None:
        return Decision.deny(Reason.NOT_FOUND)
    return evaluate(subject, Action.DATASET_QUERY.value, resource)
