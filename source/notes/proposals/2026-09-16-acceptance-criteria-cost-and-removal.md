# The acceptance-criteria call: where its seconds go, and how to get them back

**Status:** Proposal. Nothing here is built; the measurements are from live
runs on 2026-09-15/16 read through `/assistant/<run>/markdown` and the Ollama
server log.
**Date:** 2026-09-16
**Related:** `2026-07-23-reply-acceptance-criteria.md` (why the call exists),
`2026-08-31-turn-latency-and-prompt-redundancy.md` (Findings 2–4 and Proposal A
there are the same diagnosis, made from a different run; this note adds the
per-run numbers, the model-load term, the identity leak, and an order of
work), `2026-07-24-operator-locale-and-language.md` (read before touching
anything about reply language).

## The question

`acceptance_criteria` is the first model call of every assistant turn. In the
run that prompted this note (`4d43cccf…`, request "what os are you running
on") it took 17.1 s of a 37 s turn. The operator's standing todo says: reduce
it, ideally eliminate it; it yields nearly the same text every turn; it
sometimes invents rules (address the user by their preferred name); and it may
be part of why replies come out in English. Each of those is examined below,
with numbers.

## Anatomy of one call

Step line from the run:

```
Step 2 · warm-up · gemma4:e4b · t0.15 c20k struct · in 4650 · out 231 · cached 0
· prefill 6.2s · decode 4.8s · 285 tok/s · took 17.1s · 03:07:30
```

Prefill plus decode is 11.0 s. The other 6.1 s is not in the step line at all.
The Ollama log explains it:

```
time=2026-09-16T03:07:36.313+02:00 … msg="llama-server started in 5.78 seconds"
```

The run started at 03:07:29; the criteria call at 03:07:30; the model finished
loading at 03:07:36. The call was the first thing to touch the model after an
idle gap, so it paid the load. So the 17.1 s is three separable terms:

| Term | Time | What decides it |
|---|---|---|
| Model load | 5.8 s | Ollama had unloaded gemma4:e4b (default `keep_alive` is 5 min); the first call after idle pays it |
| Prefill, 4650 tokens, 0 cached | 6.2 s | Prompt size; the previous request on the model (last turn's summarizer) shares no prefix with this prompt |
| Decode, 231 tokens | 4.8 s | Output size; roughly 90 % of it restates the formatting guide |

Nothing in the call is "thinking" in a sense that scales with the request's
difficulty. A six-word question and a paragraph cost the same.

## The same shape across twelve runs

The last twelve runs on the overview page, criteria step only. `took` is the
step's wall-clock; `extra` is `took − prefill − decode`.

| run | in | out | cached | prefill | decode | took | extra |
|---|---|---|---|---|---|---|---|
| 4d43cccf | 4650 | 231 | 0 | 6.2 | 4.8 | 17.1 | 6.1 |
| 7bbb5331 | 4662 | 260 | 0 | 6.1 | 4.7 | 13.6 | 2.8 |
| f62703a9 | 4785 | 229 | 0 | 6.4 | 4.3 | 17.0 | 6.3 |
| 6bbff829 | 5009 | 202 | 0 | 6.6 | 3.7 | 10.4 | 0.1 |
| cdd2ab32 | 5051 | 172 | 0 | 6.6 | 3.1 | 12.5 | 2.8 |
| 2be37930 | 5042 | 279 | 0 | 6.6 | 5.2 | 18.4 | 6.6 |
| a39d3215 | 4904 | 227 | 0 | 6.5 | 4.2 | 13.7 | 3.0 |
| d491631e | 4627 | 278 | 0 | 6.1 | 5.1 | 14.2 | 3.0 |
| 1a7153f4 | 4655 | 223 | 0 | 6.1 | 4.0 | 12.9 | 2.8 |
| 04ba9d96 | 4679 | 237 | 0 | 6.1 | 4.3 | 10.6 | 0.2 |
| 8b5ae612 | 4805 | 244 | 0 | 6.3 | 4.5 | 11.0 | 0.2 |
| b493782a | 5048 | 258 | 0 | 6.6 | 4.9 | 11.7 | 0.2 |

Three things are constant and one is bimodal:

- **Prefill is 6.1–6.6 s every time, and `cached` is always 0.** The prompt
  is ~4.7 k tokens whatever the request, and none of it is ever reused: the
  request before it on the model is the previous turn's `run_summarizer`,
  whose prompt shares nothing with this one. The SWA-cache fix from the
  latency note is still in force (`using full-size SWA cache`, 20480 cells in
  the log), so this is not a cache regression; it is that the first call of a
  turn has nothing to inherit.
- **Decode is 3–5 s for 170–280 tokens.** Output length varies with how
  verbosely the model paraphrases the guide, not with the request.
- **The extra term is 0.1–0.3 s or 2.8–6.6 s, nothing in between.** The high
  values are model loads (each matches a `llama-server started in 5.8–7.1
  seconds` line: 22:20, 22:59, 23:21, 03:07). The 2.8–3.0 s cases are most
  likely the same mechanism with the weights still in the page cache — the
  log also shows loads of ~1 s, and a fresh context allocation comes on
  top; I did not pair those runs with their log lines. Every turn that
  starts more than five minutes after the previous one pays this, and it
  lands in the criteria call only because that call is first.

So for a turn after a pause the call is ~17 s; back-to-back it is ~11 s. The
operator's "12 s, between a quarter and a half of the turn" is that steady
state.

## What the call produces, and what consumes it

The structured result has three prose fields. From the run:

- `processing`: "adopt the user's preferred units of metric, the timezone of
  Europe/Copenhagen, and the currency of DKK, as specified in the user
  settings."
- `formatting`: dates YYYY-MM-DD, Monday weeks, 24-hour clock, Copenhagen
  time, decimal point without thousands separators, DKK, "The language must
  be en-US."
- `assumptions`: one sentence about what "what os are you running on" leaves
  open.

The first two are a paraphrase of `<formatting_guide>`, which the same prompt
showed the model one section earlier, and which the decide prompt and the
audit prompt also contain verbatim (`user_profile/formatting.py` renders it
deterministically from the nine locale fields). The language line copies
`reply_language_markdown`, which the classifier produced before this call and
which decide and audit also receive. Only `assumptions` is information the
code did not already hold. The latency note's Finding 3 says exactly this;
the twelve runs above are twelve confirmations: the field texts differ only in
wording.

Downstream, the criteria block is optional everywhere. `_criteria_markdown`
is injected into the decide prompt (`agents/assistant.py`, the
`if self._criteria_markdown:` at each builder), the second-opinion prompt, and
the reply audit, each guarded by that `if`. The `assistant.acceptance_criteria`
slot's own description says an unbound or failing slot leaves the turn to run
"with no criteria section rather than failing", and step 0 is written
fail-open. Unbinding it is therefore a supported configuration, not a hack.

The one thing the block delivers that nothing else does is `assumptions`
fixed *before* decide runs. In the run at hand decide's own `reason` field
restated it ("a question about my nature as an AI model … I must explain this
limitation"), which is the usual case: the ambiguity a 4B criteria model can
see, the same 4B decide model sees.

## The preferred-name rule

The criteria prompt carries `<user_settings_yaml>` whole:

```
"full_name": "…", "preferred_name": "…", "handle": "…", "gender": "…",
"about": "…", "birthday": "…", "address": "…", "email": "…",
"units": "metric", "temperature": "celsius", "timezone": "Europe/Copenhagen", …
```

The call's job is constrained by the turn instructions to units, timezone,
currency, separators, dates, spelling. It has no use for a name, a handle, a
birthday, or a street. But a small model told to "restate every line that
bears on this reply" and handed a `preferred_name` will, some turns, decide
that using it is a formatting requirement — which is how "The response must
use the user's preferred name" ends up in a criteria block, and from there
in the audit's checklist. That is not a policy the operator set; it is a
field the operator filled in for other purposes, read by a call that should
never have seen it.

Two fixes, not exclusive:

- **Structural.** Give the criteria call (or whatever replaces it) only the
  locale fields. With the latency note's Proposal B, the guide already carries
  those, so the call needs no `user_settings_yaml` at all.
- **Guidance.** A profile-guidance line stating when the assistant may use
  the name (never, except for something genuinely important) steers the
  decide and audit prompts too, which also see the name and also sometimes
  use it. This is the operator's own suggestion and works with today's code;
  it just relies on the model honouring a sentence rather than on the sentence
  being impossible to get wrong.

## Does the call push replies into English?

The suspicion: the criteria block is English prose injected into the reply
prompt, and it restates "The language must be en-US", so a Danish request
meets three English voices telling it what to do.

What the evidence supports:

- Reply language is decided by the classifier before the criteria call and
  restated in the guide's Language line; in the run above it resolved `en-US`
  by detection without asking a model, which is the designed path.
- The locale note measured language failures as a *reply model* property (a
  4B mixes languages, a 9B holds them) and got its wins from routing, not
  from prompt language. It carries no measurement in which an English system
  prompt produced an English reply to a non-English request that a
  translated prompt fixed.
- The criteria block is the only one of the three restatements that is fresh
  model prose. Removing it (any option below) deletes one English voice for
  free.

What translating the system prompts would cost: about twenty tuned prompts to
maintain in each language; instruction-following on small local models is
strongest in English, so the translated variants would need their own evals;
the shared prefix that the prompt cache reuses would split by language, so a
Danish turn after an English one would pay full prefill on every call. None of
that is worth doing on a suspicion. If the question matters, the `language`
eval family already exists to settle it with numbers; run it before and after
option 1 below and see whether anything moves.

## Options

Ordered by effort. Savings are per turn against the twelve-run figures. Every
option keeps the shared-prefix structure intact — nothing here changes the
shape of a prompt that another call reuses.

### 1. Unbind the slot (no code)

On `/agentmodel`, unbind `assistant.acceptance_criteria`. Step 0 records
"skipped", no criteria section is injected anywhere.

- Saves the whole call: 10–18 s.
- Costs decide the prefix the criteria call primed: decide showed
  `cached 2566` in this run, and those tokens now prefill at ~750 tok/s, so
  ~3 s back. Net 8–15 s.
- Loses the one-sentence `assumptions` fixed before step 1. decide's `reason`
  carries the same content in the common case; the audit still checks the
  reply against the guide and the settings, which it receives directly.
- Reversible in one click. Run it for a week and read the reply-audit
  verdicts on `/assistant-overview`; if ambiguous requests start getting
  answered instead of asked about, that is the signal to do option 3 rather
  than to re-bind.

### 2. Keep the model resident (no code)

Set `OLLAMA_KEEP_ALIVE=-1` (or `24h`) for the Ollama app, or pass
`keep_alive` on the requests. Removes the 3–6 s load from the first call of a
turn after an idle gap — which is the criteria call today and would be the
decide call after option 1, so this is worth doing either way. Cost: the
weights stay resident (gemma4:e4b is small; on a unified-memory machine this
is a non-issue). The latency note listed it as step 4; the twelve runs say it
is worth more than that position suggests, because most of the operator's
turns *are* first-after-idle.

### 3. Render the deterministic half in code, then fold the rest into decide

This is the latency note's Proposal A followed by its Proposal D, and it is
the version of "eliminate it" that keeps what the call was for.

1. Code fills `processing` and `formatting` from the criteria snapshot profile
   and the resolved language — the same renderer the guide uses, so the two
   can never disagree. The model is asked for `assumptions` only. Output
   falls from ~230 tokens to ~40 (decode ~1 s instead of ~4.5 s). The
   "restate every line" and "the language is not yours" paragraphs leave the
   turn instructions, which trims the prompt too. The identity fields leave
   the prompt, which closes the preferred-name leak by construction.
2. Once that is stable, move the single remaining field into the decide
   schema: step 1's decision gains an `assumptions` field the model fills
   before choosing its action, and the injected block is built from it for
   the audit. The separate call disappears; the ambiguity note survives; the
   cost is ~40 output tokens on the decide call, under a second.

Verification is offline, as the latency note describes: replay recorded runs
through `evals/profile_guidance.py`, diff the model's `processing`/`formatting`
against the rendered version, and confirm the diff is paraphrase.

### Not an option: shrinking the criteria call's history

It looks like one — the call gets the whole transcript (~2 k tokens of the
4.7 k here) to resolve a pronoun — and the latency note once listed it as
Proposal C. It is ruled out here because the prompts are built for the cache:
criteria and decide are byte-identical from `<current_user_request>` through
`<formatting_guide>`, with the history inside that shared run, and decide
reuses the prefix up to the first byte that differs. A shorter history in the
criteria call moves that byte earlier, so decide prefills the whole transcript
again from scratch; the audit sits behind decide and pays the same way. The
seconds saved on the criteria call come back on the two calls after it, with
interest. Any change to what the criteria call sees must keep its prompt a
prefix of decide's, which means: remove whole calls (options 1 and 3), never
trim inside a shared section.

## Recommended order

1. Today: option 2, then option 1. Both are settings; together they take the
   run above from ~37 s to roughly 20 s.
2. After a week of audit verdicts: if the missing `assumptions` never shows
   up as a problem, leave it unbound and delete the call's code when
   convenient. If it does, build option 3, which restores the note at the
   cost of one short field on decide.
3. Independently: the profile-guidance line about the name, since decide and
   audit see `preferred_name` regardless of what happens to the criteria call.
4. Leave the system prompts in English; run the `language` evals if the
   suspicion persists after the criteria block is gone.

## How to verify

- `/assistant/<run>/markdown`, "Model calls" table: after option 1 the
  criteria row is absent; after option 2 the first model call's `took` is
  within ~0.5 s of its `prefill + decode`.
- `~/.ollama/logs/server.log`: no `llama-server started` lines between turns
  once `keep_alive` is set.
- `/assistant-overview`: reply-audit verdicts over the trial week, compared
  with the week before.
- `evals/profile_guidance.py` for option 3's replay diff.
