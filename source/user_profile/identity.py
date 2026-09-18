"""Operator identity block: who the operator *is*, from the current profile.

The `profile.current` setting points at one profile on the /profile page (a
person profile — the operator's own "account"). This module renders that
profile's filled-in fields into a compact prompt block the assistant injects
as `<user_settings_yaml>`, next to the memory-derived `<user_profile>`
digest: identity is declared once by the operator, the digest accrues from
remembered claims.

Rendering is registry-driven (`profile_fields.PROFILE_FIELDS`) and emits YAML:
fields appear under their registry keys in registry order, absent/blank fields
are skipped, and the connector-owned `dynamic` subtree is never rendered. YAML
because it is the same mapping with fewer tokens — no braces, quotes, or
commas around values a model reads as prose anyway — while keeping the
guarantees the block relies on: every value is emitted by `yaml.safe_dump`, so
a field containing newlines or quotes cannot forge structure (multi-line
values render as literal blocks, values that would read as another type are
quoted), the registry keys stay stable machine identifiers, and the round
trip through `yaml.safe_load` is exact (`user_profile/export.py` depends on
that to rebuild the document from this string).

The formatting guide rides the same block as YAML comments: each directive
sits on the line of the field it derives from (`date_format: YYYY-MM-DD  #
Example 2026-12-31`), and a field whose stored value is an opaque enum shows
the guide's display form instead (`temperature: Celsius (°C)`, also when
the guide derived it from the units). The comments are code-owned text whose
interpolated values passed `user_profile/formatting.py`'s validators; they
are invisible to the YAML parser, so the round trip above still holds.
"""

import logging
from typing import Any
from uuid import UUID

import yaml

import db
from profile_fields import PROFILE_FIELDS
from user_profile.formatting import FormattingGuide

logger = logging.getLogger(__name__)


class _BlockDumper(yaml.SafeDumper):
    """SafeDumper that renders a multi-line string as a literal block (`|`)
    instead of a quoted scalar full of escapes: an address reads as an
    address. Single-line strings keep the default style, which quotes only
    what would otherwise parse as a number, date, or boolean."""


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_BlockDumper.add_representer(str, _represent_str)


def dump_block(payload: Any) -> str:
    """A prompt block's YAML: key order kept, unicode kept, no line folding,
    no document markers, no trailing newline — a bare mapping or list the
    enclosing tag names. Shared by the identity and knowledge blocks."""
    return yaml.dump(
        payload, Dumper=_BlockDumper, allow_unicode=True, sort_keys=False,
        default_flow_style=False, width=10**6,
    ).rstrip("\n")


def current_profile() -> dict[str, Any] | None:
    """The profile selected by the `profile.current` setting (full data blob,
    via profile_get), or None when the setting is unset or the uuid no longer
    resolves (e.g. the profile was deleted after being selected). App context
    required (reads the setting from the DB)."""
    raw = db.get_setting("profile.current")
    if not raw:
        return None
    try:
        target = UUID(str(raw).strip())
    except ValueError:
        logger.warning("profile.current is not a uuid: %r", raw)
        return None
    profile = db.profile_get(target)
    if profile is None:
        logger.warning("profile.current points at unknown profile %s", target)
    return profile


def _comment(text: str) -> str:
    """One comment's text as it goes on the line: whitespace collapsed to
    single spaces, so it can never contain the newline that would end the
    comment early and turn its tail into YAML."""
    return " ".join(text.split())


def _field_lines(key: str, value: str, comment: str) -> list[str]:
    """One field dumped on its own, the comment appended to its first line.
    A single-line scalar makes `key: value # comment`; a multi-line value
    renders as a literal block and the comment follows the block indicator
    (`key: |- # comment`), which YAML allows — the indented lines below
    stay content. A value cannot start a comment of its own: the dumper
    quotes any scalar containing ` #`."""
    lines = dump_block({key: value}).splitlines()
    if comment:
        lines[0] = f"{lines[0]} # {comment}"
    return lines


def format_identity_block(profile: dict[str, Any],
                          guide: FormattingGuide | None = None) -> str:
    """Render one profile as a prompt block: a YAML mapping of the filled-in
    fields under their registry keys, in registry order. No preamble line
    and no profile display name: the enclosing <user_settings_yaml> tag
    names the content, and the tree label is operator
    bookkeeping (it rides the per-step debug log, not the prompt). This is
    the single place to experiment with identity prompt formatting.

    `guide` is the formatting guide to render as comments (see the module
    docstring); None renders the fields alone. One comment renders whether
    or not a guide is given: an enum value's registry gloss
    (`number_format`'s separators, `address_as`'s rule) follows the value —
    looked up from the validated enum value, never operator text, so it
    cannot smuggle instructions into this context-authority block."""
    data = profile.get("data") or {}
    comments: dict[str, str] = dict(guide.comments) if guide else {}
    shown: dict[str, str] = dict(guide.values) if guide else {}
    lines: list[str] = []
    for field in PROFILE_FIELDS:
        value = shown.get(field.key) or str(data.get(field.key) or "").strip()
        if not value:
            continue
        parts = [comments.get(field.key, ""), field.glosses.get(value, "")]
        comment = _comment(" ".join(p for p in parts if p))
        lines.extend(_field_lines(field.key, value, comment))
    return "\n".join(lines)


def build_identity_block() -> str:
    """The operator identity prompt block, or "" when no current profile is
    set — so callers can inject unconditionally without a stray header."""
    profile = current_profile()
    if profile is None:
        return ""
    return format_identity_block(profile)
