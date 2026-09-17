"""Knowledge-calibration prompt block: the operator's self-declared per-topic
calibration rows rendered as a YAML list.

Injected by the main assistant as `<user_expertise_yaml>` — a bare tag, like
`<user_settings_yaml>` beside it; the shared system prompt declares both
reference data, never instructions, and carries the reading rules (explicit
requests override, unlisted topics carry no inference, the axis meanings),
so the block is data alone: one mapping per row, in stored order, each enum
value followed by a short gloss of what it asks for (`_GLOSS`). Only the
omission disclosure, when rows were dropped to fit the budget, rides along —
as a YAML comment on the last line, so a parser sees the rows and a reader
sees the count.

Rows are `yaml.safe_dump` output through the identity block's dumper, not
hand-built prose: a topic or note containing a newline, quote, colon, or
dash stays one scalar and cannot forge a second row or key. Server-owned ids
and stamps never enter the prompt.

There is deliberately no topic matching or aliasing here: the whole block is
injected and the model performs synonym resolution natively ("Postgres" hits a
"PostgreSQL" row). Aliases belong to a future routing design.
"""

import logging
from typing import Any

from db.profile_calibration import calibration_rows
from user_profile.identity import dump_block

logger = logging.getLogger(__name__)

# One global budget across the formatting-guide and calibration BODIES:
# formatting is admitted first, calibration uses the remainder. A storage cap
# and a prompt cap — not the fiction that all 100 stored rows render at full
# fidelity in every turn.
MAX_PROFILE_GUIDANCE_CHARS = 2_700

# The prompt-visible row fields, in serialization order. Never id/updated_at.
_FULL_KEYS = ("topic", "level", "stance", "depth", "note")
_COMPACT_KEYS = ("topic", "level", "stance")

# Each enum value carries a four-to-five-word gloss in the prompt — "expert
# (omit routine fundamentals)" rather than a bare "expert" — because the
# models this runs on are small and read a phrase more reliably than a token
# whose meaning sits elsewhere. The phrases are the shared system prompt's
# own definitions, shortened; a value outside the vocabulary renders as-is.
_GLOSS: dict[str, dict[str, str]] = {
    "level": {
        "expert": "omit the routine fundamentals",
        "intermediate": "normal depth, explain unusual parts",
        "beginner": "define terms, expose assumptions",
        "none": "start from first principles",
    },
    "stance": {
        "prefer": "lean toward it when equal",
        "neutral": "no steering either way",
        "avoid": "do not build on it",
    },
    "depth": {
        "concise": "short answers, essentials only",
        "standard": "the normal explanation depth",
        "teach": "explain thoroughly, step by step",
    },
}


def glossed(key: str, value: str) -> str:
    gloss = _GLOSS.get(key, {}).get(value)
    return f"{value} ({gloss})" if gloss else value

OMISSION_PREFIX = "# Omitted "


def _yaml_row(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    """One row as a one-item YAML list ("- topic: …" then indented keys), so
    rows concatenate with newlines into one list."""
    payload = {k: glossed(k, str(row[k])) for k in keys
               if str(row.get(k) or "").strip()}
    return dump_block([payload])


def _omission_line(count: int) -> str:
    """The disclosure, as a YAML comment: parsers skip it, readers see it."""
    return (f"{OMISSION_PREFIX}{count} declared topics that did not fit; they "
            "are declared, just not shown here.")


def _assemble(rows: list[dict[str, Any]], budget: int) -> tuple[str, int]:
    """One degrade-then-drop pass under `budget`. Returns (body,
    omitted_count) WITHOUT the omission line — the caller reserves space for
    it and appends the real one.

    Phase 1 admits full rows in operator priority order while they fit; every
    later row starts in compact form. While the total still exceeds the
    budget, cuts are taken in strict preference order: (1) drop the last
    compact non-avoid row — later rows absorb cuts first; (2) with only
    avoid rows left to drop, DEGRADE the last still-full row to compact
    instead — shrinking an earlier row is always better than un-declaring an
    operator's `avoid`; (3) only when nothing can shrink further, drop the
    last avoid row. An avoid the model never sees is the worst truncation
    outcome."""
    used = 0
    # entry: {index, full, compact, is_avoid, mode}
    entries: list[dict[str, Any]] = []
    full_mode = True
    for index, row in enumerate(rows):
        full_line = _yaml_row(row, _FULL_KEYS)
        compact_line = _yaml_row(row, _COMPACT_KEYS)
        if full_mode and used + 1 + len(full_line) <= budget:
            mode = "full"
            used += 1 + len(full_line)
        else:
            full_mode = False
            mode = "compact"
        entries.append({
            "index": index, "full": full_line, "compact": compact_line,
            "is_avoid": str(row.get("stance") or "") == "avoid",
            "mode": mode,
        })

    def _line(entry: dict[str, Any]) -> str:
        return entry["full"] if entry["mode"] == "full" else entry["compact"]

    def _total() -> int:
        return sum(1 + len(_line(e)) for e in entries) - (1 if entries else 0)

    omitted = 0
    while entries and _total() > budget:
        compact_entries = [e for e in entries if e["mode"] == "compact"]
        victim = next((e for e in reversed(compact_entries)
                       if not e["is_avoid"]), None)
        if victim is not None:
            entries.remove(victim)
            omitted += 1
            continue
        degradable = next((e for e in reversed(entries)
                           if e["mode"] == "full"), None)
        if degradable is not None:
            degradable["mode"] = "compact"
            continue
        entries.pop()          # only avoid rows remain; drop from the end
        omitted += 1
    return "\n".join(_line(e) for e in entries), omitted


def format_calibration(profile: dict[str, Any],
                       max_chars: int = MAX_PROFILE_GUIDANCE_CHARS) -> str:
    """Render one profile's calibration rows as the prompt-block body under
    `max_chars` (the caller passes the global guidance budget minus the
    formatting guide it already admitted). Deterministic; no DB access.
    Returns "" when no topics are stored.

    Degrade-then-drop, so overflow can never silently cancel a declared
    preference: full rows while they fit, then compact rows
    (topic/level/stance — notes and depth dropped, truncated before
    serializing, never cut mid-row), then omission from the end with
    avoid rows dropped last — and the final line states the exact number
    omitted, with its space reserved before the final row is admitted so the
    disclosure cannot itself break the cap."""
    rows = calibration_rows(profile.get("data"))
    if not rows:
        return ""
    body, omitted = _assemble(rows, max_chars)
    if not omitted:
        return body
    # Something was dropped: redo the pass with space reserved for the
    # worst-case omission line, then disclose the exact count. The smaller
    # budget can degrade rows earlier and thereby fit MORE of them — when the
    # second pass omits nothing after all, the disclosure line is not owed.
    reserve = 1 + len(_omission_line(len(rows)))
    body, omitted = _assemble(rows, max(0, max_chars - reserve))
    if not body:
        return ""
    if not omitted:
        return body
    return f"{body}\n{_omission_line(omitted)}"


def build_calibration_block() -> str:
    """Convenience wrapper for tests and ad-hoc callers: renders the active
    profile under the full guidance budget, "" when none is selected. NEVER
    wire this into the main handle path — that path renders from its one
    context snapshot and threads the formatting guide's remainder through
    format_calibration."""
    from user_profile.identity import current_profile

    profile = current_profile()
    if profile is None:
        return ""
    return format_calibration(profile)
