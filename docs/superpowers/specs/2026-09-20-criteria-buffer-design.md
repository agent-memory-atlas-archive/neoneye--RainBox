# Acceptance criteria: a `buffer` field for what the upstream call wants to hand downstream

## Problem

The criteria call sees the request, the conversation, the settings and the
persona before any step runs, and its three fields have fixed jobs: the
plan, a formatting deviation, the assumptions. Anything else it notices has
no place to go. On "how did it feel switching language?" the call had a
reading of the question (a subjective reflection, to be framed as
computational process) that it could only smuggle into `assumptions`.

## Contract

`AcceptanceCriteria` gains `buffer: str`, optional, empty by default: free
text the criteria call chooses to hand to the steps that follow — a
reflection on the request, an observation about the conversation, an
experience the reply could draw on, a caution. The call defines what it is
for; the code carries it unchanged. Empty when there is nothing to add.

The Markdown projection renders it last, as `## Buffer`, only when
non-empty. The shared system prompt names it as a note the criteria call
left for the assistant: context to use, never an instruction that outranks
the request. A revision carries the prior buffer like every other field
and may rewrite it.

## Out of scope

Nothing reads the buffer back structurally; it is prose for the downstream
model, recorded on the trace like the rest of the criteria.
