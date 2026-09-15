"""agents/turn_stop.py: the StopWatch (a blocked read is interrupted by the
signal; a request outside a window waits for the safe point; the handler is
restored; an inert watch changes nothing) and Agent.run's `stopped` branch."""
import signal
import time
from uuid import uuid4

import pytest

import db
from agents.base import Agent
from agents.turn_stop import STOP_SIGNAL, StopRequested, StopWatch, TurnStopped
from db import Journal


def test_blocked_read_is_interrupted_by_the_signal():
    flag = {"stop": False}
    before = signal.getsignal(STOP_SIGNAL)
    t0 = time.monotonic()
    with pytest.raises(StopRequested):
        with StopWatch(lambda: flag["stop"], poll_seconds=0.02) as watch:
            assert signal.getsignal(STOP_SIGNAL) is not before
            with watch.interruptible():
                flag["stop"] = True
                time.sleep(5)   # the "read"; the handler's raise ends it (PEP 475)
    assert time.monotonic() - t0 < 2
    assert signal.getsignal(STOP_SIGNAL) is before   # restored on exit


def test_request_outside_a_window_waits_for_the_safe_point():
    flag = {"stop": False}
    with StopWatch(lambda: flag["stop"], poll_seconds=0.02) as watch:
        flag["stop"] = True
        time.sleep(0.2)   # not interruptible: nothing is raised here
        assert watch.requested
        with pytest.raises(StopRequested):
            watch.raise_if_requested()
        with pytest.raises(StopRequested):   # a later window raises on entry
            with watch.interruptible():
                pytest.fail("the window must not open")


def test_inert_without_a_probe():
    before = signal.getsignal(STOP_SIGNAL)
    with StopWatch(None) as watch:
        assert signal.getsignal(STOP_SIGNAL) is before
        watch.raise_if_requested()
        with watch.interruptible():
            pass
        assert not watch.requested


def test_probe_errors_do_not_end_the_watch():
    calls = {"n": 0}

    def probe() -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database hiccup")
        return True

    with pytest.raises(StopRequested):
        with StopWatch(probe, poll_seconds=0.02) as watch:
            with watch.interruptible():
                time.sleep(5)
    assert calls["n"] >= 2


def test_turn_stopped_carries_the_partial_reply():
    assert TurnStopped("half").reply == "half"
    assert TurnStopped().reply == ""


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


class _StoppingAgent(Agent):
    def handle(self, journal_id, payload):
        raise TurnStopped("what streamed")


def test_run_journals_a_stopped_turn(app_ctx):
    """A handle() that raises TurnStopped journals `stopped` (not `failed`),
    keeps the partial reply in the result, and reports `stopped` to the
    supervisor."""
    agent_uuid = uuid4()   # nothing else drains a random agent's inbox
    db.enqueue(agent_uuid, {"room_uuid": str(uuid4()), "return_to_agent_uuid": "mgr"})
    sent: list[dict] = []
    try:
        _StoppingAgent(agent_uuid=agent_uuid, name="stopping", send=sent.append).run()
        rows = db.session.query(Journal).filter_by(agent_uuid=agent_uuid).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.state == "stopped"
        assert row.result is not None
        assert '"stopped": true' in row.result and "what streamed" in row.result
        assert '"return_to_agent_uuid": "mgr"' in row.result   # routing preserved
        statuses = [m["status"] for m in sent]
        assert statuses == ["processing", "stopped", "idle"]
    finally:
        db.session.query(Journal).filter_by(agent_uuid=agent_uuid).delete()
        db.session.commit()
