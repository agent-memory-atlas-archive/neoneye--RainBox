"""Tests for the direct-room helpers in db/chat.py: room_type on
create_chatroom, set_chatroom_settings, and edit_chat_message.

Uses the live local Postgres database. Every test cleans up rows it
created so artifacts don't accumulate.
"""

import json
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

import db
from db import Chatroom, Inbox, Journal


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        ctx.pop()


@pytest.fixture
def direct_room(app_ctx):
    human = db.get_human_user()
    assert human is not None, "seed_chat_defaults should have run"
    room = db.create_chatroom(
        f"direct-test-{uuid4().hex[:6]}", human.uuid, [], room_type="direct"
    )
    try:
        yield room.uuid, human.uuid
    finally:
        db.session.query(Chatroom).filter(Chatroom.uuid == room.uuid).delete()
        db.session.commit()


@pytest.fixture
def agents_room(app_ctx):
    human = db.get_human_user()
    assert human is not None
    room = db.create_chatroom(f"agents-test-{uuid4().hex[:6]}", human.uuid, [])
    try:
        yield room.uuid, human.uuid
    finally:
        db.session.query(Chatroom).filter(Chatroom.uuid == room.uuid).delete()
        db.session.commit()


def test_create_chatroom_defaults_to_agents(agents_room):
    room_uuid, _human = agents_room
    room = db.get_chatroom(room_uuid)
    assert room.room_type == "agents"
    assert room.system_prompt == ""
    assert room.model_uuid is None


def test_create_chatroom_direct(direct_room):
    room_uuid, _human = direct_room
    assert db.get_chatroom(room_uuid).room_type == "direct"


def test_create_chatroom_rejects_invalid_room_type(app_ctx):
    human = db.get_human_user()
    with pytest.raises(ValueError):
        db.create_chatroom("bad-type", human.uuid, [], room_type="bogus")


def test_list_chatrooms_carries_room_type_and_model(direct_room):
    room_uuid, _human = direct_room
    entry = next(r for r in db.list_chatrooms() if r["uuid"] == str(room_uuid))
    assert entry["room_type"] == "direct"
    assert entry["model_uuid"] is None


def test_set_chatroom_settings_roundtrip(direct_room):
    room_uuid, _human = direct_room
    model_uuid = uuid4()
    db.set_chatroom_settings(
        room_uuid, system_prompt="Be terse.", model_uuid=model_uuid
    )
    room = db.get_chatroom(room_uuid)
    assert room.system_prompt == "Be terse."
    assert room.model_uuid == model_uuid
    # Partial update: only the passed field changes.
    db.set_chatroom_settings(room_uuid, system_prompt="Be verbose.")
    room = db.get_chatroom(room_uuid)
    assert room.system_prompt == "Be verbose."
    assert room.model_uuid == model_uuid
    # model_uuid=None clears the model.
    db.set_chatroom_settings(room_uuid, model_uuid=None)
    assert db.get_chatroom(room_uuid).model_uuid is None


@pytest.fixture
def stored_prompt(app_ctx):
    """One /prompt row to link rooms to."""
    from db.models import Prompt
    row = Prompt(uuid=uuid4(), name="Pirate", content="You are a pirate.")
    db.session.add(row)
    db.session.commit()
    try:
        yield row.uuid
    finally:
        db.session.query(Prompt).filter(Prompt.uuid == row.uuid).delete()
        db.session.commit()


def test_set_chatroom_settings_prompt_link(direct_room, stored_prompt):
    room_uuid, _human = direct_room
    db.set_chatroom_settings(room_uuid, prompt_uuid=stored_prompt)
    assert db.get_chatroom(room_uuid).prompt_uuid == stored_prompt
    # Partial update elsewhere leaves the link alone.
    db.set_chatroom_settings(room_uuid, system_prompt="free text")
    assert db.get_chatroom(room_uuid).prompt_uuid == stored_prompt
    # prompt_uuid=None unlinks.
    db.set_chatroom_settings(room_uuid, prompt_uuid=None)
    assert db.get_chatroom(room_uuid).prompt_uuid is None


def test_resolve_room_system_prompt(direct_room, stored_prompt):
    room_uuid, _human = direct_room
    # Unlinked: the room's own free text.
    db.set_chatroom_settings(room_uuid, system_prompt="Be terse.")
    assert db.resolve_room_system_prompt(db.get_chatroom(room_uuid)) == "Be terse."
    # Linked: the stored version's content wins over the free text.
    db.set_chatroom_settings(room_uuid, prompt_uuid=stored_prompt)
    assert db.resolve_room_system_prompt(
        db.get_chatroom(room_uuid)) == "You are a pirate."
    # Linked version deleted: no system message (NOT the stale free text).
    from db.models import Prompt
    db.session.query(Prompt).filter(Prompt.uuid == stored_prompt).delete()
    db.session.commit()
    assert db.resolve_room_system_prompt(db.get_chatroom(room_uuid)) == ""


def test_set_chatroom_settings_rejects_agents_room(agents_room):
    room_uuid, _human = agents_room
    with pytest.raises(ValueError):
        db.set_chatroom_settings(room_uuid, system_prompt="nope")


def test_set_chatroom_settings_missing_room(app_ctx):
    with pytest.raises(LookupError):
        db.set_chatroom_settings(uuid4(), system_prompt="x")


def test_edit_chat_message_updates_text_and_content_type(direct_room):
    room_uuid, human_uuid = direct_room
    msg = db.post_chat_message(room_uuid, human_uuid, "hello", "markdown")
    db.edit_chat_message(msg.id, '{"now": "json"}')
    row = db.get_room_message(room_uuid, msg.id)
    assert row["text"] == '{"now": "json"}'
    assert row["content_type"] == "json"


def test_delete_chat_message_removes_row(direct_room):
    room_uuid, human_uuid = direct_room
    msg = db.post_chat_message(room_uuid, human_uuid, "delete me")
    keep = db.post_chat_message(room_uuid, human_uuid, "keep me")
    db.delete_chat_message(msg.id)
    ids = [r["id"] for r in db.list_room_messages(room_uuid)]
    assert msg.id not in ids
    assert keep.id in ids


def test_delete_chat_message_guards(direct_room):
    room_uuid, human_uuid = direct_room
    with pytest.raises(LookupError):
        db.delete_chat_message(-1)
    streaming = db.post_chat_message(room_uuid, human_uuid, "part", streaming=True)
    with pytest.raises(ValueError):
        db.delete_chat_message(streaming.id)


def test_delete_chat_message_any_settled_kind(direct_room):
    """Every settled row is deletable — the operator owns a direct room's
    whole transcript, notices and thinking rows included."""
    room_uuid, human_uuid = direct_room
    for kind in ("thinking", "notice"):
        row = db.post_chat_message(room_uuid, human_uuid, "x", kind=kind)
        db.delete_chat_message(row.id)
        assert db.get_room_message(room_uuid, row.id) is None


def test_edit_chat_message_guards(direct_room):
    room_uuid, human_uuid = direct_room
    with pytest.raises(LookupError):
        db.edit_chat_message(-1, "x")
    thinking = db.post_chat_message(
        room_uuid, human_uuid, "hmm", kind="thinking"
    )
    with pytest.raises(ValueError):
        db.edit_chat_message(thinking.id, "x")
    streaming = db.post_chat_message(
        room_uuid, human_uuid, "part", streaming=True
    )
    with pytest.raises(ValueError):
        db.edit_chat_message(streaming.id, "x")


def test_set_chatroom_settings_history_window(direct_room):
    """The rolling window: how many kind='message' rows the model sees.
    Null (the default) = the whole room; partial updates leave it alone."""
    room_uuid, _human = direct_room
    assert db.get_chatroom(room_uuid).history_window is None
    db.set_chatroom_settings(room_uuid, history_window=12)
    assert db.get_chatroom(room_uuid).history_window == 12
    db.set_chatroom_settings(room_uuid, system_prompt="unrelated")
    assert db.get_chatroom(room_uuid).history_window == 12
    db.set_chatroom_settings(room_uuid, history_window=None)
    assert db.get_chatroom(room_uuid).history_window is None


# ---- the Stop button's queue side -------------------------------------------

def _drop_agent_rows(agent_uuid):
    db.session.query(Inbox).filter_by(agent_uuid=agent_uuid).delete()
    db.session.query(Journal).filter_by(agent_uuid=agent_uuid).delete()
    db.session.commit()


def test_cancel_room_turns_drops_queued_and_flags_processing(direct_room):
    """Only this room's items go: the queued one is deleted, the one a worker
    took gets stop_requested_at (kept on a second press), another room's item
    is untouched. The watcher's own-session read sees the flag."""
    room_uuid, _ = direct_room
    other_room = uuid4()
    agent = uuid4()   # a throwaway agent uuid: no supervisor drains it
    db.enqueue(agent, {"room_uuid": str(room_uuid)})
    db.enqueue(agent, {"room_uuid": str(other_room)})
    db.enqueue(agent, {"room_uuid": str(room_uuid)})
    try:
        taken = db.take_item(agent)          # the oldest: this room's first item
        assert taken is not None
        journal_id, _payload = taken
        assert not db.stop_requested(journal_id)
        assert db.cancel_room_turns(room_uuid, agent) == {"dequeued": 1, "signalled": 1}
        assert db.stop_requested(journal_id)
        left = [json.loads(r.payload)["room_uuid"] for r in
                db.session.query(Inbox).filter_by(agent_uuid=agent).order_by(Inbox.id)]
        assert left == [str(other_room)]
        stamp = db.session.get(Journal, journal_id).stop_requested_at
        assert db.cancel_room_turns(room_uuid, agent) == {"dequeued": 0, "signalled": 1}
        assert db.session.get(Journal, journal_id).stop_requested_at == stamp   # not re-stamped
        with Session(bind=db.db.engine) as side:   # as the worker's watcher reads it
            assert db.stop_requested(journal_id, session=side)
        assert not db.stop_requested(uuid4())
        assert db.cancel_room_turns(other_room, agent) == {"dequeued": 1, "signalled": 0}
    finally:
        _drop_agent_rows(agent)


def test_settle_streaming_rows_flips_only_that_senders_rows(direct_room):
    room_uuid, human_uuid = direct_room
    responder = uuid4()
    mine = db.post_chat_message(room_uuid, responder, "half a", kind="message", streaming=True)
    think = db.post_chat_message(room_uuid, responder, "hmm", kind="thinking", streaming=True)
    done = db.post_chat_message(room_uuid, responder, "earlier", kind="message")
    other = db.post_chat_message(room_uuid, human_uuid, "typing", kind="message", streaming=True)
    assert db.settle_streaming_rows(room_uuid, responder) == [mine.id, think.id]
    by_id = {r["id"]: r for r in db.list_room_messages(room_uuid)}
    assert by_id[mine.id]["streaming"] is False and by_id[mine.id]["text"] == "half a"
    assert by_id[think.id]["streaming"] is False
    assert by_id[done.id]["streaming"] is False
    assert by_id[other.id]["streaming"] is True     # another sender's row is not ours
    assert db.settle_streaming_rows(room_uuid, responder) == []
