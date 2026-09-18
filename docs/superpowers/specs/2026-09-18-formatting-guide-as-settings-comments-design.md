# Formatting guide as comments inside `<user_settings_yaml>`

## Goal

The assistant's prompts carry the operator's profile twice: once as data
(`<user_settings_yaml>`, the profile fields as YAML) and once as directives
derived from the same fields (`<formatting_guide>`, a bullet list: "Dates:
YYYY-MM-DD, for example 2026-12-31; do not use month-first dates"). The
guide is the settings restated. This change folds the guide into the
settings block as YAML comments next to the field each directive derives
from, and removes the `<formatting_guide>` block from every call.

Before:

```
<user_settings_yaml>date_format: YYYY-MM-DD
number_format: '1234567.89'
number_format.comment: Don't show thousand separators. Use DOT as decimal separator.
currency: DKK</user_settings_yaml>
<formatting_guide>Use these defaults unless the current request or exact source notation says otherwise:
- Dates: YYYY-MM-DD, for example 2026-12-31; do not use month-first dates.
- Numbers: decimal point without thousands separators, for example: 1234567.89
- Currency: use the currency code DKK with the preferred number format, for example 1234.56 DKK. Convert currencies only with a supplied or freshly retrieved rate.
- Language: reply in the language of the current message; never switch on your own. Use en-US only when the message asks for it; an explicit request always wins. When writing en, use the en-US variant — spelling and vocabulary alike; never mix in another variant of the same language.</formatting_guide>
```

After:

```
<user_settings_yaml># The comments are formatting defaults; the current request or exact source notation overrides them.
date_format: YYYY-MM-DD  # For example 2026-12-31; do not use month-first dates.
number_format: '1234567.89'  # Don't show thousand separators. Use DOT as decimal separator.
currency: DKK  # For example 1234.56 DKK. Convert currencies only with a supplied or freshly retrieved rate.
# Language: reply in the language of the current message; never switch on your own. Use en-US only when the message asks for it; an explicit request always wins. When writing en, use the en-US variant — spelling and vocabulary alike; never mix in another variant of the same language.</user_settings_yaml>
```

One block instead of two, every directive next to the value it explains,
and the value itself is the example where it used to be repeated.

## Rendering

`user_profile/formatting.py` keeps its lookup tables and prompt-boundary
validation but returns a `FormattingGuide` value instead of a body string:

- `comments: dict[str, str]` — registry field key → one comment sentence
  (no leading `#`). Keys: `date_format`, `first_day_of_week`, `time_format`,
  `timezone`, `units`, `temperature`, `currency`, `currency_2`.
- `language: str` — the language directive, which has no field of its own
  in the block (language rows are not rendered there), so it renders as a
  trailing comment line prefixed `Language:`.
- `chars` — total comment length, the number the shared guidance budget
  deducts before the calibration block takes the remainder.

Wording moves from "- Dates: YYYY-MM-DD, for example …" to "For example …":
the key names the topic and the value is the example, so the comment
states only what the value does not. Two directives that used to share a
line or derive from another field split or move:

- Time and timezone were one "Times:" line; they are two fields and get
  two comments. Timezone alone still says "Present local times in …".
- A temperature derived from `units` when the `temperature` field is unset
  has no key to sit on; it joins the `units` comment ("Temperature in
  Celsius (°C).") so the derived default is not lost.
- A currency whose primary value fails validation still promotes the
  secondary: the comment attaches to whichever key holds the first valid
  code, and the invalid raw value stays in the block uncommented (as it
  does today) with the same warning logged.

`number_format` already carried a code-owned `<key>.comment` entry so a
small model could read the opaque sample. That entry becomes a comment on
the `number_format` line and stays independent of the switch, exactly as
the `.comment` key was. The guide adds nothing further for numbers: the
old "Numbers:" line only restated the value. `NUMBER_FORMATS` keeps only
the per-minor-unit currency examples.

`user_profile/identity.py` gains the comment placement:
`format_identity_block(profile, guide=None)`. Each field is dumped on its
own through the existing `_BlockDumper`, and the comment is appended to the
field's first line as `  # …`. When the guide has anything, the block opens
with one header comment (the old guide header, reworded for its new place)
and closes with the language comment when there is one. Comments are
code-owned text; every interpolated value passed the same validators as
before, and a defensive collapse of whitespace guarantees a comment can
never contain a newline. A field value cannot end a comment or start one:
YAML quotes any scalar containing ` #`, and multi-line values render as
literal blocks whose lines are content. `yaml.safe_load` still parses the
block exactly (comments are invisible), so `user_profile/export.py` is
unchanged apart from no longer seeing a `number_format.comment` key.

## Assistant assembly

`_build_declared_profile_blocks` returns `(identity, calibration)`; the
guide is rendered when `assistant.formatting_guide` is on and passed into
`format_identity_block`. There is no `_formatting_block`, no `"formatting"`
static block, and no `_criteria_formatting_guide`: the criteria call gets
the same identity block every other call gets. Its former bypass of the
switch goes with it — one switch, one rendering, one block.

Static-block sets become: classifier / recall filter / criteria
{identity, calibration}, audit the same, second opinion {+profile}, decide
{+persona}; criteria adds `assistant_persona`. The audit call's head is now
identical to the classifier's, and criteria and decide share the run
through `user_expertise_yaml` (and `assistant_persona` when decide's
`user_profile` is empty).

Prompt text that named the removed tag changes only enough to point at the
comments instead:

- `SOURCE_PRIORITY_SECTION` and its criteria variant: the `formatting_guide`
  rank becomes `user_settings_yaml (the user's settings; its comments are
  the default formatting, which the current request and exact source
  notation override)`, and `user_settings_yaml` leaves the lower shared
  rank. The block keeps the guide's authority position rather than dropping
  to the profile's, so a persona sentence cannot outrank the language rule.
- The shared system prompt sentence "The formatting_guide holds the active
  profile's formatting defaults." → "The comments in user_settings_yaml hold
  the active profile's formatting defaults."
- The criteria turn instructions: "Read the formatting guide line by line"
  → "Read the formatting comments in user_settings_yaml line by line".
- The audit turn instructions' item 4 and the `reply` action's description
  drop the separate mention of the guide.

The `assistant.formatting_guide` setting keeps its name and meaning (on:
the comments render; off: only the `number_format` comment, as before), as
does the turn log's `formatting_guide: on|off` entry. The eval harness's
`include_formatting` variant flag maps onto the same switch.

## Tests

- `user_profile/test_formatting.py`: the golden Germany rendering becomes a
  dict of comments plus the language line; every "line is independent" test
  reads a key instead of searching a body.
- `user_profile/test_identity.py`: comment placement, header and trailing
  language line, round trip through `yaml.safe_load`, a hostile value
  containing ` #` stays inside its scalar, and a multi-line value with a
  comment still parses.
- `agents/test_assistant_formatting_guide.py`, `test_assistant_prompt_tiers.py`,
  `evals/test_profile_guidance.py`: no `<formatting_guide>` anywhere; the
  switch decides whether `user_settings_yaml` carries the comments; the
  criteria and decide prompts share the identity block byte for byte.

## Out of scope

`<user_settings_languages_json>` (the classifier's own block) and the
calibration block are untouched.
