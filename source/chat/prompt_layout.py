"""Prompt layouts — how a chat-facing model call arranges its messages.

Every layout sends the same material; they differ in where the user's request
sits relative to everything else.

- `single_turn`: the system prompt, then ONE user message holding everything —
  history, settings, memories, the call's instructions — with the request as
  one section inside it.
- `request_turn`: the system prompt, a user message holding the context (the
  same sections minus the request), a fixed assistant acknowledgement, then the
  request itself, verbatim, as the final user message. The chat template
  already marks the last user turn as the thing to act on; this layout gives
  that position to the operator's own words instead of to a context blob.

The `prompt.layout` setting picks the layout. Each agent reads it once per turn
so every call of one turn uses one layout. A new layout is a new constant here,
appended to PROMPT_LAYOUTS (which is the setting's `choices`), plus the branch
in each agent that builds it.

Design: notes/proposals/2026-10-19-request-as-its-own-turn.md.
"""

import logging

logger = logging.getLogger(__name__)

SINGLE_TURN = "single_turn"
REQUEST_TURN = "request_turn"

PROMPT_LAYOUTS: tuple[str, ...] = (SINGLE_TURN, REQUEST_TURN)
DEFAULT_PROMPT_LAYOUT = SINGLE_TURN

# The assistant turn between the context and the request. Written in the
# model's own voice and stating what it will do, because a model stays
# consistent with what it has already said; a request for more ("now provide
# the prompt") would commit it to nothing. "Message", not "prompt": the person
# on the other end writes a message. One constant for every call that uses the
# layout — each call's own job is in its instructions, and "act on" covers
# replying, choosing an action and establishing criteria alike.
REQUEST_TURN_ACKNOWLEDGEMENT = (
    "Context received. I'll treat it as reference material and act on your "
    "next message."
)


def request_turns(request_text: str) -> list[dict[str, str]]:
    """The turns that follow the context message: the acknowledgement, then
    the request. `{role, content}` dicts — the shape the rejected-attempt
    feedback turns already use, so the trace stores both alike."""
    return [
        {"role": "assistant", "content": REQUEST_TURN_ACKNOWLEDGEMENT},
        {"role": "user", "content": request_text},
    ]


def current_prompt_layout() -> str:
    """The `prompt.layout` setting, read once per turn by each agent.

    Best-effort: an unreadable or unknown value reads as the default, so a
    settings failure never changes how a prompt is built on its own."""
    try:
        import db

        value = db.get_setting("prompt.layout")
    except Exception:
        logger.warning("prompt.layout setting read failed", exc_info=True)
        return DEFAULT_PROMPT_LAYOUT
    return str(value) if value in PROMPT_LAYOUTS else DEFAULT_PROMPT_LAYOUT
