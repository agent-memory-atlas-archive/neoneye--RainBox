# Stop button for direct-chat turns — design

**Status: accepted design; implementation follows on this branch.** A direct room's composer gets a **Stop** button
that aborts the model turn in flight — the operator hit Send with a typo, or
the model under trial is too slow for the question — and the machinery
behind it actually stops the model, not just the page.

## What "stop" means

Pressing Stop in a direct room cancels **every** pending direct-chat turn for
that room: the item being processed and any items still queued behind it
(two quick sends before regret). Nothing further is generated until the
operator sends again. Concretely:

- The worker's model stream is interrupted, including while it is still
  waiting for the first token from a cold model. The stream generator is
  closed, which closes the HTTP connection; the inference servers in use
  (Ollama, LM Studio, llama.cpp-style OpenAI-compatible servers) stop
  generating when the client disconnects, so the GPU is released too.
- Whatever text already streamed stays in the room, its rows settled
  (`streaming=false`, no stuck cursor). A partial reply is still an
  assistant turn in the transcript; the operator can edit or delete it like
  any other row.
- A `kind="notice"` row "⏹ Stopped — after 12s (model X)" marks the cut. It
  is posted by the direct-chat agent, so it reaps the working bubble the
  same way a reply or a failure notice does; notices are excluded from
  transcripts, so the model never sees it.
- The journal row records state `stopped` (a state the queue already
  defines and nothing used) with the partial reply in its result.

Send keeps working while a turn is in flight: a follow-up message queues a
second turn behind the first, as today. Stop is a separate control, not a
mode of the Send button.

## Where the request lives: the journal

The cancel intent is one nullable column on the queue's own record of the
work: `journal.stop_requested_at TIMESTAMPTZ`. It is set on the row being
processed and read back by the worker that owns it. This is the queue-level
counterpart of the assistant's `assistant_control` rows: the assistant needs
step-granular control (stop, redirect) with a trace; a direct turn is one
model call, and "please stop this item" is all it needs. A journal row is
per turn, so a stale request can never leak into the next turn, and the API
needs no separate bookkeeping to expire it.

Queued items (in `inbox`, not yet taken) are simply deleted. `take_item` and
the API race for the same row; whichever commits first wins, and the other
path handles the item's new state (it is either gone, or now a processing
journal row that gets flagged).

## API

`POST /chat/api/rooms/<uuid>/stop` — direct rooms only (403 otherwise;
404 for an unknown room). It calls `db.stop_direct_chat_turn(room_uuid)`:

1. Delete every inbox item of the direct-chat agent whose payload names this
   room (`dequeued`).
2. Set `stop_requested_at = now()` on every `processing` journal row of the
   direct-chat agent whose payload names this room and that has no request
   yet (`signalled`; idempotent — pressing twice re-signals nothing).
3. If nothing was signalled, no worker is going to close the turn, so the
   endpoint settles the room itself: every still-`streaming` row of the
   direct-chat agent in the room is marked settled, and the stop notice is
   posted (reaping the working bubble). This is also the recovery path for
   a row left streaming by a worker that died: pressing Stop clears the
   stuck cursor.

Response: `{"ok": true, "dequeued": n, "signalled": m, "settled": bool}`.

Reading the room out of the JSON payload happens in Python over the few
rows the direct-chat agent ever has queued or in flight; no index or JSON
query is needed.

## Worker: `agents/turn_stop.py`

Two pieces, kept generic so another single-call agent can adopt them:

- `StopRequested(Exception)` — raised in the worker's main thread when the
  operator's request is observed.
- `StopWatch` — a context manager the agent enters around the model call.
  On entry it installs a `SIGUSR1` handler (main thread only, previous
  handler restored on exit) and starts a daemon thread that evaluates a
  caller-supplied `is_requested()` every 0.5 s. When it returns true the
  thread sets an event and, if the main thread has declared itself
  interruptible, delivers `SIGUSR1` to it with `signal.pthread_kill`.

The signal is how a blocked read is interrupted. A handler that raises makes
the interrupted system call propagate the exception instead of retrying
(PEP 475) — the mechanism the supervisor's SIGTERM already relies on to
unwind a worker mid-read. The handler raises only while the main thread is
inside `watch.interruptible()`, which the agent wraps around the two calls
that can block on the network: opening the stream (`stream_chat`, which for
a cold model blocks until the server starts answering) and each `next()` on
it. Outside those windows the handler does nothing; the agent checks
`watch.requested` between chunks (its safe point) and raises there. So a
request that lands during a database flush is honoured a few milliseconds
later at the next chunk boundary, never inside a commit. Entering an
interruptible window with the event already set raises immediately, and the
set-flag/check-event pair is done under a lock so a request cannot fall
between the check and the read.

Teardown order matters: the interruptible flag is cleared, then the watcher
thread is stopped and joined, and only then is the previous `SIGUSR1`
handler restored — a late signal could otherwise hit the default action,
which terminates the process.

The poll reads `journal.stop_requested_at` on its own `Session(bind=engine)`
(the engine captured in the main thread), never `db.session`: the scoped
session belongs to the main thread and its app context, and sharing it from
a thread breaks the caller's transaction (the activity recorder documents
the same rule).

## Agent flow (`agents/direct_chat.py`)

`_stream_reply` opens a `StopWatch` bound to the turn's journal id, wraps
the stream open and each `next()` in `interruptible()`, and checks
`requested` between chunks. On `StopRequested` it closes the generator
(`stream.close()`, best effort), finishes the writer (partial text kept,
rows settled — with the same `</think>` answer recovery as a normal
finish), posts the stop notice, and re-raises as `TurnStopped`. If no journal
id is known (tests calling `_stream_reply` directly), the watcher is inert.

`Agent.run` (`agents/base.py`) gains one branch beside the `failed` one:
`TurnStopped` journals the item `stopped` with `{"ok": false,
"stopped": true, "reply_content": <partial>}` (routing preserved like a
failure) and emits `{"status": "stopped"}` to the supervisor, which treats
it like `completed`/`failed` (journal id cleared). Stopped rows are not
routed by `fetch_unrouted_terminal`; no routed agent can be stopped today.

The `llm_call` activity row for the aborted call records the exception like
any other mid-stream failure; that is accurate — the call did not complete.

## Client (`webapp/chat_template.py`)

- Markup: `<button type="button" id="stop-btn" hidden>⏹ Stop</button>` in the
  composer before Send, styled as the composer's secondary (grey) button.
- **In-flight detection** is derived from the rendered log, not a separate
  flag: a turn is in flight when the open room is direct and the log holds
  a `progress` row or a row with the streaming cursor. Rows carry
  `data-kind` for this. `syncStopButton()` runs after every path that
  changes the log: append, streaming upsert, deletion, room switch, folder
  view. The button is hidden otherwise, so agents rooms never show it.
- **Click**: label becomes "Stopping…" and the button disables; POST
  `/stop`; on error the label is restored and a toast says why. On success
  the button stays in "Stopping…" until the next sync finds the turn gone
  (the notice arrives over SSE and the streaming rows settle). A
  `fetchNew` follows the POST so the settled-by-API path shows its notice
  without waiting for the stream.
- No keyboard shortcut: Escape already cancels inline edits and modals on
  the page.

## Schema

`init_db` adds `journal.stop_requested_at` with `_add_column_if_missing`;
`create_all` builds it on a fresh database. The admin Journal view lists it.

## Tests

- `agents/test_turn_stop.py` — the watcher: a blocked generator (sleeping
  inside `interruptible()`) is interrupted; a request outside the window is
  honoured at the next `requested` check; the handler is restored; entering
  a window after a request raises at once.
- `agents/test_direct_chat.py` — a stubbed stream that is stopped after two
  chunks: partial text settled, stop notice posted, working bubble reaped,
  `TurnStopped` raised; `Agent.run` journals `stopped`.
- `db/test_chat_direct.py` — `stop_direct_chat_turn`: dequeues only this
  room's items, flags only this room's processing journal, idempotent,
  settles stuck streaming rows when nothing is in flight.
- `webapp/test_chat_direct_api.py` — the endpoint (403 for agents rooms,
  response shape, notice on the settled path).
- `webapp/test_chat_views.py` — markers: the button, `syncStopButton`, and
  that every log-changing path calls it.

## Files

| Area | File |
| --- | --- |
| Stop watcher, `StopRequested`, `TurnStopped` | `agents/turn_stop.py` |
| Journal column, stop helper | `db/models.py`, `db/queue.py`, `db/chat.py`, `db/__init__.py` |
| `stopped` journal branch | `agents/base.py` |
| Interruptible stream, notice | `agents/direct_chat.py` |
| Endpoint | `webapp/chat_api.py` |
| Button + sync | `webapp/chat_template.py` |
| Supervisor status | `core.py` |
| Operator docs | `notes/direct-chat.md`, `notes/chat-frontend-rules.md`, `README.md` |
