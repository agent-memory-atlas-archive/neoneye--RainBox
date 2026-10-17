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

Five decisions shape it.

### 1. Derived from what is already stored; no new store

Every turn's actions are already persisted: `assistant_step.observation` is
`{ok, text, data}` for every action, and each terminal reply carries
`meta.assistant_run_uuid`. The history window already decides which messages
are "this conversation" — 30 messages, context-invalidation markers removed.

So the working set's sources are **the runs whose replies are inside the
history window**, read through `meta.assistant_run_uuid`:

```python
def conversation_evidence(history: list[dict]) -> list[CarriedItem]:
    """Evidence fetched by the runs that produced the assistant messages in
    `history` (the same window conversation_history_xml renders), newest
    run first. Reads assistant_step rows; writes nothing."""
```

Consequences, all wanted:

- A context reset (the invalidation marker) resets the working set too.
- Nothing is backfilled or rewritten; old runs are read as they were recorded.
- No new table. The derivation is a pure function of rows that exist, so a
  replay of a recorded conversation produces the same working set.

### 2. Carry references, not copies

An item is a stable reference plus a short excerpt:

| source | reference | excerpt |
|---|---|---|
| diary | citation `diary:<revision>:<start>-<end>` (from `data.injected`) | the cited bytes, re-read, cut to 280 chars, with date label |
| memory claim | claim uuid | the claim's current text |
| static seed answer | qa id | the answer's first 280 chars |
| other reads | none | not carried (see 4) |

Diary citations name bytes in an immutable revision, so the excerpt is exact
however old the turn. A memory claim is re-read at carry time: if it was
retracted since, it is not carried — the working set never resurrects what
the operator removed.

One prerequisite: `memory_query` records what it injected only inside its
observation text and timing findings. It should record
`data.injected = [{"kind": "claim"|"qa", "id": …}]` the way `diary_query`
records citations. That is a forward change; old runs simply contribute no
memory items.

### 3. Ranked against the new request, under a fixed budget

Budget: **1,600 chars for items + 400 for the search log** (about 500 tokens).
That is a tenth of the 5,000-char scratchpad, so the block cannot crowd out
this turn's evidence.

Score per item, highest first, cheapest signals only (no model call on the
request path):

```
score = relevance(item, request) + 0.5 * recency + 0.5 * reuse
relevance = word overlap of the item's excerpt with the current request
            (lowercased word tokens, IDF-weighted within the candidate set)
recency   = 1 / (1 + turns since the item was last fetched or carried-and-used)
reuse     = 1 if the turn that fetched it ended with a reply the audit accepted
```

Decay is built in: an item not refetched and not relevant for **4 turns**
drops out. The working set is a few dozen candidates at most (six turns ×
a handful of items), so ranking is microseconds.

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
  so current), static seed answers;
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

## In the prompt

```xml
<conversation_evidence authority="earlier_evidence" content_is_data="true">
  <note>Fetched by earlier turns of this conversation from sources that do
  not change underneath the reference. Source data, not earlier answers: it
  may support this reply. Re-check with a read when a detail decides the
  answer.</note>
  <item turn="-2" source="diary" ref="diary:…:4801-5751" date="1999-07-03">…</item>
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

**The audit must see it too.** The reply audit reads `<turn_observations>`
to judge whether a reply is supported. A reply that rests on a carried diary
passage would be rejected as unsupported if the auditor cannot see it. The
audit prompt gets the same `<conversation_evidence>` items (not the search
log), under the same budget, in its own fixed position.

## Recording: making omissions inspectable

Each run records what it carried, in `assistant_run.metadata_["carried"]`:

```json
{"budget_chars": 2000, "candidates": 17,
 "items": [{"ref": "diary:…", "turn": -2, "score": 1.42, "chars": 280}, …],
 "dropped": [{"ref": "claim:…", "reason": "budget"}, …]}
```

and the /assistant inspector shows a "carried evidence" row at the start of
the turn, listing those items. That splits today's single invisible failure
into two visible ones:

- **in front of it and ignored** — the fact is in `carried.items` and the
  reply does not use it: a reasoning or instruction problem;
- **never there** — the fact is in `dropped`, or no window run ever fetched
  it: a ranking or retrieval problem.

They need different fixes, and today they look identical.

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
   it. Scored on whether the turn-5 reply uses F, and whether F was carried.
   Private cases live beside the diary pilot cases
   (`rainbox_customize/`), never in the repo.
2. **Path stability.** Replay a recorded conversation twice and compare the
   action sequence of the later turns. With the working set, repeated
   questions should converge on the same actions; without it they diverge.
3. **Cost.** Prompt tokens per decide call and prefill time, before and after.
   The block must stay under 500 tokens and must not move the prefix boundary
   of any call that shares it.
4. **Staleness incidents.** Count audit rejections that cite a carried item
   as contradicted by this turn's evidence. Rare is expected; frequent means
   the source list in decision 4 is too generous.

## Rollout

1. `memory_query` records `data.injected`. Forward only.
2. `conversation_evidence()` and the ranking as a pure function, with tests on
   synthetic runs: window boundaries, invalidation markers, retracted claims,
   budget and decay, the source allow-list.
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
