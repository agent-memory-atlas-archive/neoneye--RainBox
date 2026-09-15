"""Queue persistence: agent inbox + journal.

Split out of db.py. Holds the inbox/journal queue operations (enqueue,
take_item, journal_update, fetch_unrouted_terminal, mark_routed,
agent_uuids_with_work). Re-exported from db for import compatibility.
"""
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from db.models import VALID_STATES, Inbox, Journal, State, db


def enqueue(agent_uuid: UUID, payload: dict[str, Any]) -> None:
    db.session.add(
        Inbox(
            agent_uuid=agent_uuid,
            payload=json.dumps(payload),
        )
    )
    db.session.commit()


def take_item(agent_uuid: UUID) -> tuple[UUID, dict[str, Any]] | None:
    """Atomically pop the oldest inbox item for this agent and start a journal
    entry in 'processing' state. Returns (journal_id, payload_dict) or None.
    `journal_id` is a uuid (see the Journal model)."""
    row = (
        db.session.query(Inbox)
        .filter_by(agent_uuid=agent_uuid)
        .order_by(Inbox.id.asc())
        .first()
    )
    if row is None:
        return None
    inbox_id = row.id
    enqueued_at = row.enqueued_at
    payload_str = row.payload
    db.session.delete(row)
    now = datetime.now(UTC)
    journal = Journal(
        inbox_id=inbox_id,
        agent_uuid=agent_uuid,
        enqueued_at=enqueued_at,
        started_at=now,
        updated_at=now,
        state="processing",
        payload=payload_str,
    )
    db.session.add(journal)
    db.session.commit()
    return journal.id, json.loads(payload_str)


def journal_update(
    journal_id: UUID,
    state: State,
    result: dict[str, Any] | None = None,
) -> None:
    if state not in VALID_STATES:
        raise ValueError(f"invalid state {state!r}; must be one of {VALID_STATES}")
    row = db.session.get(Journal, journal_id)
    if row is None:
        raise LookupError(f"journal row {journal_id} not found")
    row.state = state
    row.updated_at = datetime.now(UTC)
    row.result = json.dumps(result) if result is not None else None
    db.session.commit()


def fail_journal_if_processing(
    journal_id: UUID, result: dict[str, Any]
) -> bool:
    """Fail an abandoned journal without overwriting a terminal result."""
    row = db.session.get(Journal, journal_id)
    if row is None or row.state != "processing":
        return False
    row.state = "failed"
    row.updated_at = datetime.now(UTC)
    row.result = json.dumps(result)
    db.session.commit()
    return True


def fetch_unrouted_terminal() -> list[dict[str, Any]]:
    """Terminal (completed OR failed) journal rows not yet routed, oldest first.

    Failed rows are included deliberately: a row is routed when its result
    carries an explicit `_routing.return_to_agent_uuid`, and that is exactly
    when a turn that errored still has to wake the manager waiting on it.

    Carries `state` and `result` so the supervisor can read that return address
    without a second query."""
    rows = (
        db.session.query(Journal)
        .filter(Journal.state.in_(("completed", "failed")), Journal.routed_at.is_(None))
        .order_by(Journal.started_at.asc())  # uuid id isn't monotonic
        .all()
    )
    return [
        {
            "id": r.id,
            "agent_uuid": r.agent_uuid,
            "state": r.state,
            "payload": json.loads(r.payload) if r.payload else None,
            "result": json.loads(r.result) if r.result else None,
        }
        for r in rows
    ]


def mark_routed(journal_id: UUID) -> None:
    row = db.session.get(Journal, journal_id)
    if row is None:
        raise LookupError(f"journal row {journal_id} not found")
    row.routed_at = datetime.now(UTC)
    db.session.commit()


def agent_uuids_with_work() -> set[UUID]:
    """UUIDs whose inbox currently has at least one pending item."""
    rows = db.session.query(Inbox.agent_uuid).distinct().all()
    return {r[0] for r in rows}


def _payload_names_room(payload: str | None, room_key: str) -> bool:
    try:
        return (json.loads(payload or "") or {}).get("room_uuid") == room_key
    except (ValueError, AttributeError):
        return False


def cancel_room_turns(room_uuid: UUID, agent_uuid: UUID) -> dict[str, int]:
    """The Stop button, queue side: drop every queued item of `agent_uuid`
    whose payload names `room_uuid` (`dequeued`), and flag every processing
    journal row of that agent for the room (`signalled`, the count of rows a
    worker is still on — `stop_requested_at` is set once and kept, so a
    second press changes nothing). The payloads are read in Python: the
    agent has a handful of rows at most, and the caller wants a decision,
    not a query plan. One transaction."""
    key = str(room_uuid)
    dequeued = 0
    for row in db.session.query(Inbox).filter_by(agent_uuid=agent_uuid).all():
        if _payload_names_room(row.payload, key):
            db.session.delete(row)
            dequeued += 1
    signalled = 0
    now = datetime.now(UTC)
    for row in (db.session.query(Journal)
                .filter_by(agent_uuid=agent_uuid, state="processing").all()):
        if not _payload_names_room(row.payload, key):
            continue
        signalled += 1
        if row.stop_requested_at is None:
            row.stop_requested_at = now
    db.session.commit()
    return {"dequeued": dequeued, "signalled": signalled}


def stop_requested(journal_id: UUID, *, session: Session | None = None) -> bool:
    """Whether the operator flagged this journal row. The worker's watcher
    thread polls this on a session of its own (`session`); the scoped
    db.session is never shared across threads."""
    s = session if session is not None else db.session
    stamp = s.execute(
        sa.select(Journal.stop_requested_at).where(Journal.id == journal_id)
    ).scalar_one_or_none()
    return stamp is not None
