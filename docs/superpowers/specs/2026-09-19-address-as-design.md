# How the assistant addresses the operator: `address_as`, `mention_as`, `given_name`

## Problem

The profile field `preferred_name` is a name, but its form label is
"Address them as" and the prompt block renders `preferred_name: Sarah`. A
model reads that as "call her Sarah" — and the acceptance-criteria call
turned it into "the response must use the user's preferred name" on turns
where nobody asked for a name at all. The operator's actual preference is
not a name but a policy: address me as "you"; when you speak of me, use my
handle; a given name only as a last resort. One text field cannot say that.

## Fields

`preferred_name` is replaced by three registry fields in the Identity group,
in this order: `full_name`, `native_name`, `given_name`, `handle`,
`address_as`, `mention_as`, `gender`, `about`, `birthday`.

| Key | Kind | Meaning |
|---|---|---|
| `given_name` | text | The name to use when a name is unavoidable. Declared, never guessed from `full_name` (which stays one unsplit field in any script or order). |
| `address_as` | enum `you` · `given_name` · `handle` · `full_name` | How a reply addresses the operator. |
| `mention_as` | enum `handle` · `given_name` · `full_name` | How the operator is referred to in the third person (to others, in summaries, in notes). |

Enum values carry a gloss, rendered as a comment on the value's line
exactly as the calibration block does:

```
full_name: Sarah Connor
given_name: Sarah
handle: sconnor
address_as: you # address the user as "you", never by name
mention_as: handle # refer to the user by handle
```

`Field` gains `glosses` (value → comment). An unset field renders nothing,
as every field does — the policy is the operator's to set. The `number_format`
comments that `user_profile/formatting.py` kept in its own table become that
field's glosses, so the identity block has one rule for enum comments
instead of a special case; `NUMBER_FORMAT_COMMENTS` stays as an alias of
the registry's glosses for its callers.

## Migration

A stored `preferred_name` becomes `given_name` (unless `given_name` is
already set, in which case it is dropped): an idempotent data migration in
`db/__init__.py` over every profile row, in the style of the cron-target
migration. `address_as` and `mention_as` are not written by the migration: both are
the operator's to declare on `/profile`. The built-in templates rename the key in
`data/profile_templates.json`.

## Form

No new form code: the registry drives the fieldsets. An enum option shows
its gloss after the value (`you — address the user as "you", never by
name`) so the picker explains itself; the option value stays the bare enum.

## Examples and fixtures

Hints, tests and docs use names from the Terminator films (Sarah Connor,
John Connor, Kyle Reese, Miles Dyson), never the operator's.

## Out of scope

The system prompt is unchanged: the block is data, and `address_as: you`
with its gloss states the rule itself. The two proposals that describe
`preferred_name` are dated documents and stay as written.
