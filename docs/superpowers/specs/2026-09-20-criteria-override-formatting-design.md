# Acceptance criteria: `processing` is the plan, `override_formatting` only for deviations

## Problem

The acceptance-criteria call was told to "read the formatting comments in
user_settings_yaml line by line and restate every one that bears on this
reply". On a turn where nothing deviated it produced a `formatting` field
that was the settings block rewritten as prose, and a `processing` field
that was the same settings rewritten again ("the response must adopt the
user's preferred units of metric …"). Two hundred output tokens per turn
restating what the assistant, the second-opinion reviewer and the auditor
all receive directly as `user_settings_yaml`, and a field whose name said
nothing about when it should be empty.

## Contract

`AcceptanceCriteria` keeps three prose fields with new meanings:

| Field | Required | Meaning |
|---|---|---|
| `processing` | yes, non-empty | What is about to happen to produce the reply: what will be computed, looked up, compared or asked. The plan, not the formatting. |
| `override_formatting` | no, empty by default | Filled only when this reply must deviate from the settings' defaults: the request asks for another unit, format or spelling; exact source notation must be preserved; the conversation established a convention. Says what deviates and why. Empty means the defaults apply. |
| `assumptions` | yes, non-empty | Unchanged: every ambiguity resolved from a settings default, and every ambiguity the settings cannot resolve. |

The rename carries the rule in the name: a small model reads
`override_formatting` as "only when non-default" where `formatting` read as
"describe the formatting". An empty value is a decision with one meaning
("the defaults apply"), so the required-non-empty argument that still holds
for the other two fields does not apply to it.

The Markdown projection (`acceptance_criteria_markdown`, read by the decide,
second-opinion and audit calls) renders `## Processing`, then `## Override
formatting` only when the field is non-empty, then `## Assumptions`. The
shared system prompt's sentence about the plan says that where the plan is
silent on formatting, `user_settings_yaml`'s defaults govern.

The reply language is not restated in the criteria at all: it reaches every
call as `reply_language_markdown`. The turn instructions keep the
"not yours to decide" rule and drop the "restate it in formatting" clause.

## Out of scope

The audit's check against the settings' comments, the eval harness, the
`/assistant` page (which renders the criteria JSON as stored) and the two
proposals that discuss the criteria's cost are unchanged.
