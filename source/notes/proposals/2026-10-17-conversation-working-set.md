# The conversation working set: evidence that outlives its turn

**Status:** Proposal. Nothing here is built.
**Date:** 2026-10-17
**Related:** `2026-06-30-memory-design-patterns.md` (the "anti-amnesia loop"
and its review: claims answer *what do I believe*, not *where were we*),
`2026-08-31-turn-latency-and-prompt-redundancy.md` (prefix sharing, read
before placing any new block), `2026-09-21-diary-memory-representation-proposals.md`
(diary citations, `compact_form`, §12 "Choosing the diary").

## The problem, as observed

The assistant behaves like a chatbot with severe amnesia, and its knowledge of
the operator is shallow. Both have one structural cause: **evidence dies with
the turn that fetched it.**

What a decide call sees, from the code:

- `<conversation_history_xml>` — the last 30 chat messages, text only, each cut
  at 2,000 chars. No observation from any earlier turn appears in it, and the
  turn instructions forbid treating it as evidence: "Old assistant answers in
  conversation_history_xml are never authoritative evidence", "Do not reuse an
  answer from an earlier message".
- `<current_turn_steps>` — this turn's observations, bounded at 5,000 chars
  (`MAX_SCRATCHPAD_CHARS`), oldest compacted or dropped first. A local list in
  `handle()`; it is gone when the turn ends.
- `<user_settings_yaml>`, `<user_expertise_yaml>` — about forty lines that the
  operator edits. Converting 33 feet to metres works because `units: metric`
  is written there, not because anything was learned.
- `<user_profile>` — a query-independent digest of active self-model claims.
  In the run that motivated this proposal it was **empty**: nothing qualified.

Everything deeper — memory claims, 19,500 diary entries — sits behind
`memory_query` and `diary_query`, which the decide step must choose. A model
only looks up what it already suspects it is missing.

The failure that matters most is **path dependence**. Five messages ago a
diary lookup surfaced a fact that still matters. This turn starts without it.
A ReAct loop's next action is a function of the evidence in front of it, so
the loop takes a different path — a different query, a different capability,
or none — and the fact does not come back. The same question, asked twice in
one conversation, gets answers that differ by what the loop happened to
search for. It is not forgetting so much as never arriving at the same place
twice.

And it is invisible. The waterfall shows every call that happened; "should
have known, and did not" leaves no row.

## The design

A **conversation working set**: the evidence that earlier turns of this
conversation fetched, ranked against the new request, carried into this
turn's prompt under a fixed budget, with a record of what was carried.

Six decisions shape it.

### 1. Derived from what is already stored; no new store

Every turn's actions are already persisted: `assistant_step.observation` is
`{ok, text, data}` for every action, and each terminal reply carries
`meta.assistant_run_uuid`. The history window decides which messages are
"this conversation": the last 30, with context-invalidation markers removed
from what the model reads.

So the working set's candidates are **the runs whose replies are inside the
history window and after the latest context-invalidation marker**, read
through `meta.assistant_run_uuid`:

```python
def conversation_evidence(room_messages: list[dict], ctx) -> list[CarriedItem]:
    """Evidence fetched by the runs whose replies sit in the history window
    and after the room's latest context-invalidation marker, newest run
    first, each item re-admitted under `ctx`'s current eligibility (decision
    2). Takes the room's messages BEFORE markers are stripped. Reads
    assistant_step rows; writes nothing."""
```

The boundary needs care, because a marker is not a reset. It is posted when
the active profile switches or facts are invalidated, and its own text says
"room history is preserved … re-read facts with memory_query before relying
on earlier answers". The history window therefore deliberately keeps the
messages before it, and only drops the marker row itself. Evidence is
exactly what the marker says not to rely on, so the working set takes the
**latest marker as a hard boundary**: runs before it contribute nothing,
while their messages stay in history as conversation. The boundary has to be
found before `handle()` strips the markers (the `_is_context_marker` filter);
after that it is gone.

Consequences, all wanted:

- A profile switch or a facts invalidation empties the working set; the next
  lookups rebuild it under the new context.
- Nothing is backfilled or rewritten; old runs are read as they were recorded.
- No new table. Live turns rebuild the working set from stored rows each time;
  what a past turn actually carried is recorded with that turn (see
  *Recording*), because the sources it re-read can change afterwards.

### 2. Carry references, not copies

An item is a stable reference plus a short excerpt:

| source | reference | excerpt and labels |
|---|---|---|
| diary | citation `diary:<revision>:<start>-<end>` (from `data.injected`) | the exact cited bytes via `resolve_citation`, with the renderer's full `date_label` |
| memory claim | claim uuid | the claim's current text |
| static seed answer | qa id | the answer text |
| other reads | none | not carried (see 4) |

Every excerpt, whatever its source, is at most **280 chars**, so no single
item can exceed a fixed share of the budget. The excerpt is **a window, not a
prefix**: the 280 chars of the item's full text (a diary passage is at most
600, a claim or answer can be longer) that best match the current request —
the line-aligned window with the highest word overlap, falling back to the
start when nothing matches — marked `…` at each cut end. A fact two hundred
chars into a passage is then shown when the request is about it, instead of
being cut off by a fixed prefix. Relevance (decision 3) is scored on the
**full text**, not the window, so a cut part can still select the item.

Diary citations name bytes in an immutable revision, so the excerpt is exact
however old the turn. The cited bytes alone are not enough, though: what an
excerpt *means* lives partly outside it. The renderer's `date_label` carries
the date basis (`1999-07 (month from filename)`, `date from filename`,
undated), DST ambiguity and invalid ranges, the inherited author
(`author visitor` versus `author operator (kreese)`), and the superseded-
snapshot warning — none of which is necessarily inside the cited bytes.
Dropped, a passage someone else wrote reads as a fact about the operator, a
month-level summary reads as a dated event, and corrected text appears
without its warning. So the carried form keeps the item's `date_label`
whole, in the decide prompt and in the audit input alike.

`read_citation` is the wrong reader for this. It is the model's *reader*: it
finds the entry containing the cited start and reads forward from that
entry's first passage, across later entries. Cut to 280 chars, its output can
lose the very fact that was cited — a citation into the third passage of a
long entry would come back as the entry's opening — and pin the old citation
to different bytes. The working set needs an **exact-range resolver**:

```python
def resolve_citation(citation: str, ctx: DiaryContext) -> DiaryItem | None:
    """Exactly the cited bytes, under read_citation's gates: eligible source
    (room, enabled, agent, local models), ready file, not excluded, range
    inside the revision, valid UTF-8. Labels for that range: when the cited
    revision is on the file's current chain (the current revision, or one
    whose bytes are its prefix), the date label, author and time status of
    the current generation's entry containing the cited start; otherwise the
    superseded label and no date. None when any gate fails."""
```

It shares `read_citation`'s gate and chain checks (factored out, so the two
cannot drift) and differs only in what it returns. Tests: a citation into a
later passage of a long entry returns that passage's bytes, not the entry's
start; a citation whose file was appended to since stays on the chain and
returns the same bytes with current labels; a citation whose file was edited
before the cited range comes back labelled superseded.

### 2b. Re-admitted under today's rules, not the fetching turn's

A reference that was legitimately fetched five turns ago is not
automatically legitimate now. Every carried item goes through the same
eligibility checks a fresh retrieval would apply, **before** it is ranked,
rendered or logged:

- **diary** — `resolve_citation` with the current `DiaryContext` applies
  the fresh route's gates: the room's enabled private sources only, the agent
  restriction, the local-models gate (a turn on a remote model gets no diary
  text the fresh route would refuse), file exclusions, file availability. A
  citation it rejects is not carried, and its excerpt is never logged.
- **memory claims** — membership in `hard_filtered_claims(include_secret=False,
  room_uuid, agent_uuid)` for this turn: active, not expired, not secret,
  in scope. A retracted, expired or out-of-scope claim is dropped.
- **seed answers** — the same shield filter `memory_query` applies; an answer
  behind a shield that is locked now is dropped.

The rule is the same everywhere: the working set may only show what this
turn could have fetched itself. It never resurrects what the operator
removed, and it never moves data across a boundary the fresh path enforces.

One prerequisite: `memory_query` records what it injected only inside its
observation text and timing findings. It should record
`data.injected = [{"kind": "claim"|"qa", "id": …}]` the way `diary_query`
records citations. That is a forward change; old runs simply contribute no
memory items.

### 3. Ranked against the new request, under a fixed budget

Budget: **2,000 chars for the complete serialized block** — the XML wrapper,
the note, every item with its reference and labels, and the search log —
measured with the same length function that renders it. The search log takes
at most 400 of the 2,000. Items are admitted in score order, and an item that
does not fit the remaining budget is **skipped, not a stop**: admission
continues with the next candidate until none fits. With every excerpt capped
at 280 chars this rarely matters, but it means one large leading item can
never empty the working set. For scale, that is two fifths of the 5,000-char
scratchpad, small next to the decide prompt (13–14k tokens in the latency
proposal's measurement) but not small next to this turn's own evidence, which
is why the block's rank sits below `current_turn_steps` (see *In the prompt*).

The cap is in characters because that is what can be enforced before the
call; the token cost is an estimate. Citations, uuids and XML tokenize worse
than prose, so 2,000 chars is roughly 500–700 tokens. Each run records the
block's character count, and the decide call's `prompt_eval_count` before and
after the rollout gives the measured token cost (see *Measuring it*).

Score per item, highest first, cheapest signals only (no model call on the
request path):

```
score     = relevance(item, request) + 0.5 * recency
relevance = word overlap of the item's excerpt with the current request
            (lowercased word tokens, IDF-weighted within the candidate set)
recency   = 1 / (1 + runs since the newest window run that fetched the item)
```

Both signals are computed from stored rows at the start of the turn; nothing
about an item's history has to be persisted for ranking. Whether a reply
*used* an item is deliberately not a signal: an accepted reply does not say
which retrieved items it relied on, and inferring it from text overlap would
be a guess presented as a measurement.

Lifetime follows from the derivation, with one rule:

- An item exists while a run that **fetched** it has its reply in the
  history window (and after the latest marker). Refetching renews it; when
  its last fetching run leaves the window, so does the item.
- **Carrying does not renew.** An item carried for many turns is not
  refetched by being shown, so it ages out with its fetching run. Otherwise
  an early item could keep itself alive indefinitely, and the working set
  would drift from "what this conversation recently looked at" to "what it
  looked at once, long ago". A fact that keeps mattering keeps being fetched.
- Items from the newest **6** window runs are always candidates; items from
  older window runs are candidates only when they share at least two
  IDF-weighted terms with the current request.

The working set is a few dozen candidates at most, so ranking is
microseconds.

Embedding similarity would rank better, but it costs an embedder call per
turn and the embedder shares Ollama with the decide model (a cold embedder
was 1.3 s in the run that started the waterfall fix). Lexical first; measure
before adding vectors.

### 4. Only evidence that stays true

Carried evidence is presented as **fetched earlier, still the same source
data** — not as an old answer. That distinction is what lets it coexist with
the rule that old answers are never authoritative. It holds only for sources
whose content does not change underneath the reference:

- **carried:** diary citations (immutable revisions), memory claims (re-read,
  so current), static seed answers — each only if it passes decision 2b;
- **not carried:** dynamic seed handlers (git status, project status,
  capabilities), `python_run` outputs, anything time-dependent. These are
  answers *as of then*; showing them later invites stale facts. Their
  searches still appear in the search log (5), so the loop knows they ran.

### 5. Show what was searched, not only what was found

Path dependence is as much about the queries as the results. The block ends
with a log of the read actions the window's runs made:

```
turn -2  diary_query search "toolkit first release"  → 4 passages
turn -2  memory_query "toolkit projects"             → 2 facts
turn -5  diary_query timeline 1999-07-01..1999-07-31  → 23 entries (2 pages)
```

The loop can then refine a search instead of repeating it blind, or reopen a
cursor-sized result with `read`. This also closes the diary spec's §12 gap
("the decide prompt's evidence should show whether a diary search already
ran").

**The log is evidence too, and passes the same gates.** A query is not
neutral metadata: one written after reading a diary passage can carry a name
or a fact from it. If the log copied queries unconditionally, a turn on a
remote model would receive, through the log, exactly the diary-derived text
that decision 2b keeps out of its items. Whether "the diary" is available
now is not the right test: `diary_available()` is true when *any* usable
source has a ready file, so a remote-enabled source B would unlock query text
that a local-only source A inspired. Authorising an earlier argument needs
the provenance of what its turn had read.

So each run records its **evidence scope**: the diary sources whose text was
in front of the model during that run — the sources of its `diary_query`
results (their `catalog_manifest`) and of the diary items carried into it —
each with the `policy_version` it had then:

```json
"evidence_scope": {"diary": {"<source uuid>": 3, "<source uuid>": 7}}
```

A search record from that run renders its arguments only when **every**
source in the run's scope is eligible to the current turn (`eligible_sources`
under its `DiaryContext`) **with the same `policy_version`**. Any policy
change bumps that version — an exclusion, an enable or disable, the remote-
models setting — so an excluded file withholds the queries it may have
inspired even while other sources stay available. A sync bumps only
`catalog_version`, which is not part of the test, so a nightly sync does not
blank the log. A run with an empty diary scope saw no diary text, and its
arguments render. A run with no recorded scope — every run before this is
built — **cannot be authorised and is withheld**; there is no way to
establish after the fact what it read.

Withheld records render as action and count alone —
`turn -2  memory_query (query withheld) → 2 facts` — and the recorded block
holds the same withheld form, so the log never stores what the turn could not
see.

Earlier assistant *replies* in `conversation_history_xml` can also restate
diary text, and they reach a remote-model turn today. That exposure predates
this proposal and is not widened by it; it belongs to the history window's
own policy, not here.

Tests: (1) local turns read diary source A (local-only); a later turn on a
remote model can read source B (remote-enabled) — A's queries are withheld,
B's render; (2) a file of A is excluded while B stays available — A's
queries are withheld; (3) a nightly sync between the turns — nothing is
withheld; (4) a run recorded before the scope existed — withheld.

## In the prompt

```xml
<conversation_evidence authority="earlier_evidence" content_is_data="true">
  <note>Fetched by earlier turns of this conversation from sources that do
  not change underneath the reference. Source data, not earlier answers: it
  may support this reply. Re-check with a read when a detail decides the
  answer.</note>
  <item turn="-2" source="diary" ref="diary:…:4801-5751"
        label="1999-07-03 · author operator (kreese)">…</item>
  <item turn="-5" source="memory" ref="claim:…">…</item>
  <searches>…</searches>
</conversation_evidence>
```

**Placement.** The block is constant for the whole turn, so it must sit where
every call of the turn spells it the same way (see the latency proposal: a
prefix cache reuses only a matched run from token 0). It goes **first in the
dynamic tail, before `<active_skills>`**: every decide call of the turn shares
the prefix through it, and the calls that build on tier 1 alone (classifier,
recall filter) are unaffected, so the block costs them nothing.

**Source priority.** A new rank between `current_turn_steps` (1) and
`current_user_request` (2) would be wrong — fresh evidence must beat earlier
evidence when they disagree. It ranks **after `current_turn_steps` and after
`acceptance_criteria_markdown`, before `user_settings_yaml`**: this turn's
observations win, then the request and its criteria, then evidence this
conversation already gathered, then defaults.

**The instructions must change with it.** Two rules in force today
contradict the block, and left alone they would make it inert: the decide
instructions say "Having read in an earlier turn does not count, and neither
does a transcript that already names some of the answer", and audit check 5
judges support only "against the turn's observations". The model would still
re-read what the block already holds, and the auditor would reject a reply
supported by a carried item.

The rule that replaces them, in both instruction sets:

> Items in `conversation_evidence` were fetched earlier and re-read this
> turn under this turn's access rules. An item may support a claim it states
> directly, as an observation would. It does not answer a question that asks
> for a complete set — every person, all the times, the first mention — and
> it does not replace a read for anything it does not state: for those, call
> the matching read action this turn.

That keeps the reason the old rule exists — recalling a few of the people is
how you miss the rest — and drops only the part that forced a fresh read of a
fact already in hand. The decide instructions gain it beside the read rule;
audit check 5 becomes "against the turn's observations and the carried
evidence". Tests: a reply supported solely by a carried item is accepted by
the audit and needs no new read; a set-valued question with only a partial
carried answer still triggers the read.

**The audit must see it too.** The reply audit reads `<turn_observations>`
to judge whether a reply is supported. A reply that rests on a carried diary
passage would be rejected as unsupported if the auditor cannot see it. The
audit prompt gets the same `<conversation_evidence>` items (not the search
log), labels included — an auditor that cannot see "author visitor" or
"month from filename" would approve the very misreadings decision 2 prevents —
under the same budget, in its own fixed position.

## Recording: making omissions inspectable

Each run records what it carried, in `assistant_run.metadata_["carried"]`:

```json
{"budget_chars": 2000, "block_chars": 1874, "candidates": 17,
 "boundary_marker": "<message uuid or null>",
 "block": "<the exact serialized <conversation_evidence> block>",
 "items": [{"ref": "diary:…", "turn": -2, "score": 1.42, "chars": 280,
            "claim_updated_at": null}, …],
 "dropped": [{"ref": "claim:…", "reason": "budget"},
             {"ref": "diary:…", "reason": "ineligible"}, …]}
```

**The rendered block is the historical record.** Live turns rebuild the
working set from stored rows, and that rebuild re-reads sources that change:
a claim is edited or retracted, a seed answer is revised, a shield is locked,
a diary file is excluded. Rebuilding an old turn's working set today would
therefore not show what that turn saw. The inspector, the markdown export and
any replay read `block` as recorded; references and scores are there to
explain it, and `claim_updated_at` pins the version a carried claim had.
An `ineligible` drop records only the reference and the reason — never an
excerpt the turn was not allowed to see.

The /assistant inspector shows a "carried evidence" row at the start of
the turn, listing those items. That splits today's single invisible failure
into two visible ones:

- **in front of it and ignored** — the fact is inside a carried item's
  rendered window and the reply does not use it: a reasoning or instruction
  problem;
- **reference carried, fact cut** — the item was carried, but the fact lies
  outside its window: an excerpt-selection problem;
- **never there** — the item is in `dropped`, or no window run ever fetched
  it: a ranking or retrieval problem.

They need different fixes, and today they look identical. Telling the first
two apart needs the window, so each carried item records it:
`{"ref": …, "window": [start, end], "full_chars": 512, "truncated": true}`,
with the offsets in the item's own text; the recorded `block` shows exactly
what was visible.

Telemetry follows the existing stream: carried diary citations and claims are
recorded as `retrieval_event` stage `considered`, source
`conversation.carried`, with the run's `journal_id`.

## Measuring it

The operator is the ground truth: Rainbox runs on their own data so they can
tell whether an answer about their life is right. The evaluation builds on
that, at near-zero cost per verdict.

1. **Multi-turn omission cases.** A case is a short scripted conversation:
   turn 1 asks something that makes the assistant fetch fact F; turns 2–4
   drift; turn 5 asks a question whose good answer needs F, without naming
   it. Scored on whether the turn-5 reply uses F, and on which of the three
   omission classes a miss falls into — including a case whose F sits past
   the first 280 chars of its passage, so excerpt selection is measured, not
   assumed. Private cases live beside the diary pilot cases
   (`rainbox_customize/`), never in the repo.
2. **Path stability.** Replay a recorded conversation twice and compare the
   action sequence of the later turns. With the working set, repeated
   questions should converge on the same actions; without it they diverge.
3. **Cost.** The block's recorded `block_chars` (hard cap 2,000), and the
   decide call's `prompt_eval_count` and prefill time before and after, which
   give its real token cost (estimated at 500–700). It must not move the
   prefix boundary of any call that shares it.
4. **Staleness incidents.** Count audit rejections that cite a carried item
   as contradicted by this turn's evidence. Rare is expected; frequent means
   the source list in decision 4 is too generous.

## Rollout

1. `memory_query` records `data.injected`. Forward only.
2. `conversation_evidence()` and the ranking, with tests on synthetic runs:
   window boundaries; the latest invalidation marker as a hard boundary (runs
   before it carry nothing while their messages stay in history);
   re-admission — a diary citation on a turn with a remote model, an excluded
   file, an expired or retracted or out-of-scope claim, a seed answer behind a
   locked shield, each dropped with no excerpt logged; diary labels kept
   whole (inherited author, month- and year-level dates, superseded); the
   exact-range resolver (later passage of a long entry, appended file,
   superseded file); the search log withheld across a local-to-remote
   switch; budget on the full serialized block, with an oversized leading
   claim followed by a short relevant diary item (the diary item is carried);
   lifetime (refetch renews, carrying does not, an item leaves with its last
   fetching run); the excerpt window (a fact past the first 280 chars is shown
   when the request is about it, and recorded as cut when it is not); the
   source allow-list.
3. Recording only: compute and store `metadata_["carried"]` and show the
   inspector row, **without** adding the block to the prompt. A week of runs
   shows what would have been carried and whether ranking picks the right
   items — measured before it can change a single answer.
4. The block in the decide prompt and the audit prompt, behind a setting
   (`assistant.conversation_evidence`, default off), then on by default once
   the omission cases improve and cost stays inside budget.
5. Embedding-based relevance, only if lexical ranking measurably misses.

## What this does not do

- It does not replace the always-present profile digest. `<user_profile>`
  answers "who is the operator" across conversations; the working set answers
  "what has this conversation established". The digest being empty is its own
  problem — nothing promotes the operator's facts into active self-model
  claims — and deserves its own proposal: a nightly, operator-verifiable
  digest built from memory and the diary, with provenance per line.
- It does not pre-fetch for the new request. Automatic retrieval before the
  first decide is the other half of "knowledge should not depend on the model
  choosing to look", and composes with this: pre-fetched items would enter the
  same ranked block. It is a separate change because it adds a retrieval to
  every turn's latency, where this adds only reads of stored rows.
- It does not summarise the conversation. A model-written summary is lossy and
  unverifiable; carried references are exact and checkable. A session brief
  (the memory-design proposal's "where were we") can come later and would sit
  beside this, not instead of it.
