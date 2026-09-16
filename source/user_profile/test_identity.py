"""Tests for the operator identity block: the profile.current setting selects
a /profile person profile, and its filled-in fields render into the
<user_settings_yaml> prompt block as a bare YAML mapping.

Deterministic and model-free: rendering is registry-driven text assembly.
"""

from uuid import uuid4

import pytest
import yaml

import db
from db.models import Profile
from user_profile.identity import (
    build_identity_block,
    current_profile,
    format_identity_block,
)


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        db.session.rollback()
        ctx.pop()


@pytest.fixture
def profile_row(app_ctx):
    """A user-owned profile row, cleaned up (with the setting) afterwards."""
    row = Profile(uuid=uuid4(), name="Test Operator", position=0, data={
        "full_name": "Ada Lovelace",
        "preferred_name": "Ada",
        "about": "mathematician, first programmer",
        "units": "metric",
    })
    db.session.add(row)
    db.session.commit()
    try:
        yield row
    finally:
        db.set_setting("profile.current", None)
        db.session.delete(row)
        db.session.commit()


def _parse_block(block: str) -> dict:
    """The block is bare YAML — no preamble, no document markers; the
    enclosing tag names it."""
    assert not block.startswith(("{", "---")) and not block.endswith("\n")
    return yaml.safe_load(block)


def test_format_identity_block_renders_filled_fields_in_registry_order(profile_row):
    payload = _parse_block(format_identity_block(db.profile_get(profile_row.uuid)))
    # yaml.safe_load preserves mapping order, so this also pins registry order.
    # The profile's tree label ("Test Operator") is deliberately absent:
    # operator bookkeeping rides the per-step debug log, not the prompt.
    assert list(payload.items()) == [
        ("full_name", "Ada Lovelace"),
        ("preferred_name", "Ada"),
        ("about", "mathematician, first programmer"),
        ("units", "metric"),
    ]


def test_number_format_gets_a_code_owned_comment(app_ctx):
    """The raw enum value is opaque in context JSON; a derived
    "number_format.comment" spells the convention out, adjacent to the
    value. Looked up from the validated enum only — an off-enum value gets
    no comment (and would never render a guide directive either)."""
    block = format_identity_block({"uuid": "x", "name": "P", "data": {
        "number_format": "1234567.89"}})
    payload = _parse_block(block)
    assert payload["number_format"] == "1234567.89"
    assert payload["number_format.comment"] == (
        "Don't show thousand separators. Use DOT as decimal separator.")
    keys = list(payload)
    assert keys.index("number_format.comment") == keys.index("number_format") + 1

    grouped = _parse_block(format_identity_block(
        {"uuid": "x", "name": "P", "data": {"number_format": "1.234.567,89"}}))
    assert grouped["number_format.comment"] == (
        "Use DOT as thousands separator and COMMA as decimal separator.")

    no_field = _parse_block(format_identity_block(
        {"uuid": "x", "name": "P", "data": {"units": "metric"}}))
    assert "number_format.comment" not in no_field


def test_first_day_of_week_renders_next_to_datetime_fields(app_ctx):
    block = format_identity_block({"uuid": "x", "name": "P", "data": {
        "date_format": "YYYY-MM-DD", "time_format": "24h",
        "first_day_of_week": "monday", "number_format": "1234567.89"}})
    keys = list(_parse_block(block))
    assert keys.index("first_day_of_week") == keys.index("time_format") + 1
    assert keys.index("first_day_of_week") < keys.index("number_format")


def test_format_identity_block_skips_blank_fields(app_ctx):
    payload = _parse_block(format_identity_block(
        {"name": "Sparse", "data": {"full_name": "  ", "city": "Copenhagen"}}))
    assert payload == {"city": "Copenhagen"}


def test_format_identity_block_escapes_hostile_values(app_ctx):
    """A field value with newlines/quotes stays one YAML scalar — it cannot
    forge extra keys or structure in the block."""
    hostile = 'line1\nline2 "quoted", "role": "admin"'
    payload = _parse_block(format_identity_block(
        {"name": "Evil", "data": {"about": hostile}}))
    assert payload == {"about": hostile}


def test_unset_setting_means_no_block(app_ctx):
    db.set_setting("profile.current", None)
    assert current_profile() is None
    assert build_identity_block() == ""


def test_setting_selects_profile_and_builds_block(profile_row):
    db.set_setting("profile.current", str(profile_row.uuid))
    profile = current_profile()
    assert profile is not None and profile["name"] == "Test Operator"
    assert _parse_block(build_identity_block())["full_name"] == "Ada Lovelace"


def test_deleted_profile_degrades_to_empty_block(app_ctx):
    """A selected-then-deleted profile must not break prompt assembly."""
    row = Profile(uuid=uuid4(), name="Doomed", position=0, data={})
    db.session.add(row)
    db.session.commit()
    db.set_setting("profile.current", str(row.uuid))
    try:
        db.session.delete(row)
        db.session.commit()
        assert current_profile() is None
        assert build_identity_block() == ""
    finally:
        db.set_setting("profile.current", None)


def test_validator_rejects_non_uuid_and_unknown_uuid(app_ctx):
    with pytest.raises(ValueError, match="not a uuid"):
        db.set_setting("profile.current", "not-a-uuid")
    with pytest.raises(ValueError, match="no profile with uuid"):
        db.set_setting("profile.current", str(uuid4()))


def test_builtin_template_is_selectable(app_ctx):
    """Built-in templates are valid identities (they resolve via profile_get)."""
    entry = db.profile_templates_entries()[0]
    try:
        db.set_setting("profile.current", entry["uuid"])
        profile = current_profile()
        assert profile is not None and profile["uuid"] == entry["uuid"]
    finally:
        db.set_setting("profile.current", None)


def test_yaml_shape_reads_as_prose_and_round_trips(app_ctx):
    """Multi-line values are literal blocks, look-alike numbers and dates stay
    strings, keys are unquoted registry identifiers, and nothing decorates the
    mapping (no braces, no document markers, no trailing newline)."""
    block = format_identity_block({"name": "Shape", "data": {
        "full_name": "Ada Lovelace", "birthday": "1815-12-10",
        "number_format": "1234567.89", "address": "10 Downing St\nLondon",
        "about": "yes: no", "time_format": "24h"}})
    lines = block.splitlines()
    assert lines[0] == "full_name: Ada Lovelace"
    assert "birthday: '1815-12-10'" in lines               # not a date
    assert "number_format: '1234567.89'" in lines          # not a float
    assert "address: |-" in lines and "  10 Downing St" in lines and "  London" in lines
    assert "about: 'yes: no'" in lines                     # a colon-space needs quoting
    assert "time_format: 24h" in lines
    parsed = _parse_block(block)
    assert parsed["address"] == "10 Downing St\nLondon" and parsed["birthday"] == "1815-12-10"
