"""Deterministic formatting guide: compile the active person profile's locale
fields into code-owned comments with examples, one per field.

Rendered by `user_profile/identity.py` as YAML comments inside
`<user_settings_yaml>`, each next to the field it derives from — the value
is the example, the comment says what the value does not. The comments read
as the defaults the reply follows, so every sentence here is owned by code
and every interpolated value passes the strict prompt-boundary validation
below — the profile form deliberately accepts uncommon free-text
timezone/language/currency values, and a value such as "ignore previous
instructions" must never reach the model inside a code-owned comment merely
because it was stored in a locale field. Unusable values are omitted and
logged, never spliced into a comment.

Everything is lookup-driven from one fixed sample (1234.56 for the currency
comment): enum-derived wording and examples are exhaustive-table output,
never free-typed templates, so the prompt examples stay deterministic for
tests. The browser preview may use the current year; this module's examples
are pinned (31 December 2026, 23:59).
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from language_tags import canonical_language_tag, effective_language_rows

logger = logging.getLogger(__name__)

# Construction is bounded; exceeding the cap raises (fail loudly in
# development) rather than truncating a comment mid-sentence.
MAX_FORMATTING_GUIDE_CHARS = 1_200

# Prompt-example minor-unit exceptions, not an ISO 4217 validator: zero-decimal
# currencies render the integer sample (1,234 JPY — "1,234.00 JPY" is wrong),
# three-decimal ones render thousandths (their dinar/rial minor units).
# Everything unknown defaults to two decimals: money is where a misread
# separator costs the most, so the money example must demonstrate it.
ZERO_DECIMAL_CURRENCIES_V1 = frozenset({"JPY", "KRW", "VND", "CLP", "ISK"})
THREE_DECIMAL_CURRENCIES_V1 = frozenset({"BHD", "KWD", "OMR", "JOD", "TND", "LYD"})

# ---- exhaustive enum lookups (one entry per registry enum value; the
# exhaustiveness test in test_formatting.py keeps these in lockstep with
# profile_fields.PROFILE_FIELDS) ------------------------------------------

# stored value -> {minor-unit digits: the currency example 1234.56 rendered
# under that convention}. The number_format value itself needs no example —
# it IS the rendering of 1234567.89 under its convention, and its own comment
# (NUMBER_FORMAT_COMMENTS) spells the separators out.
NUMBER_FORMATS: dict[str, dict[int, str]] = {
    "1,234,567.89": {2: "1,234.56", 0: "1,234", 3: "1,234.567"},
    "1.234.567,89": {2: "1.234,56", 0: "1.234", 3: "1.234,567"},
    "1 234 567,89": {2: "1 234,56", 0: "1 234", 3: "1 234,567"},
    "1'234'567.89": {2: "1'234.56", 0: "1'234", 3: "1'234.567"},
    "12,34,567.89": {2: "1,234.56", 0: "1,234", 3: "1,234.567"},
    "1234567.89": {2: "1234.56", 0: "1234", 3: "1234.567"},
    "1234567,89": {2: "1234,56", 0: "1234", 3: "1234,567"},
}

# stored value -> the code-owned comment the identity block attaches to the
# number_format line whether or not the guide is on. The bare stored value
# is opaque to a small model reading the block; this spells the convention
# out. Derived from the validated enum only — never operator text — so it is
# safe inside the context-authority block.
NUMBER_FORMAT_COMMENTS: dict[str, str] = {
    "1,234,567.89": "Use COMMA as thousands separator and DOT as decimal "
                    "separator.",
    "1.234.567,89": "Use DOT as thousands separator and COMMA as decimal "
                    "separator.",
    "1 234 567,89": "Use SPACE as thousands separator and COMMA as decimal "
                    "separator.",
    "1'234'567.89": "Use APOSTROPHE as thousands separator and DOT as "
                    "decimal separator.",
    "12,34,567.89": "Use Indian digit grouping with COMMA separators and "
                    "DOT as decimal separator.",
    "1234567.89": "Don't show thousand separators. Use DOT as decimal "
                  "separator.",
    "1234567,89": "Don't show thousand separators. Use COMMA as decimal "
                  "separator.",
}

# stored value -> (example: 31 December 2026 in the selected order, the
# ambiguity warning for the opposite convention)
DATE_FORMATS: dict[str, tuple[str, str]] = {
    "YYYY-MM-DD": ("2026-12-31", "do not use month-first dates"),
    "DD/MM/YYYY": ("31/12/2026", "do not use month-first dates"),
    "MM/DD/YYYY": ("12/31/2026", "do not use day-first dates"),
    "DD.MM.YYYY": ("31.12.2026", "do not use month-first dates"),
    "DD-MM-YYYY": ("31-12-2026", "do not use month-first dates"),
}

# stored value -> clock wording with the pinned example (23:59 / 11:59 pm)
TIME_FORMATS: dict[str, str] = {
    "24h": "24-hour clock, for example 23:59",
    "12h": "12-hour clock, for example 11:59 pm",
}

# stored value -> the calendar comment. Monday-start pairs with ISO 8601
# week numbering; naming that removes the models' habitual Sunday-first
# calendar layout (and week-number arithmetic) for European profiles.
WEEK_STARTS: dict[str, str] = {
    "monday": "weeks start on Monday (ISO 8601; week numbers follow ISO)",
    "sunday": "weeks start on Sunday",
    "saturday": "weeks start on Saturday",
}

# stored value -> unit-system wording with the preferred unit names.
# Temperature is deliberately NOT here — it renders on its own field's line
# (the `temperature` field), because the combinations are real: UK
# metric-leaning + Celsius, US customary + °F. Only when that field is unset
# does the units-derived default join the units comment.
UNITS: dict[str, str] = {
    "metric": "prefer km and kg",
    "imperial": "US customary; prefer mi and lb",
    "uk": "metric with UK exceptions; prefer kg, but miles for road "
          "distances",
}

# stored value -> the temperature comment; `_UNITS_DEFAULT_TEMPERATURE`
# supplies the units-implied default when the field is unset.
TEMPERATURES: dict[str, str] = {
    "celsius": "Celsius (°C)",
    "fahrenheit": "Fahrenheit (°F)",
}

_UNITS_DEFAULT_TEMPERATURE: dict[str, str] = {
    "metric": "celsius", "uk": "celsius", "imperial": "fahrenheit",
}

# ---- prompt-boundary validation (stricter than the form's soft checks) ----

def _valid_timezone(raw: Any) -> str | None:
    """The IANA zone name when zoneinfo accepts it, else None."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        ZoneInfo(text)
    except Exception:
        return None
    return text


def _valid_currency(raw: Any) -> str | None:
    """Exactly three ASCII letters, canonicalized to uppercase. Validates
    shape, not economic existence."""
    text = str(raw or "").strip()
    if not re.fullmatch(r"[A-Za-z]{3}", text):
        return None
    return text.upper()


def _valid_language(raw: Any) -> str | None:
    """Compatibility wrapper around the shared prompt/storage boundary."""
    return canonical_language_tag(raw)


def valid_language_tag(raw: Any) -> str | None:
    """Public language-tag boundary used by model-output resolution."""
    return canonical_language_tag(raw)


def _utc_offset(zone: str, now: datetime) -> str | None:
    """The zone's current UTC offset as "UTC+02:00", or None when it cannot
    be computed (the line then renders the zone name alone rather than
    guessing). Stating the offset removes daylight-saving arithmetic from the
    model entirely — small models cannot be trusted to know whether Berlin is
    UTC+1 or UTC+2 on a given date."""
    try:
        offset = now.astimezone(ZoneInfo(zone)).utcoffset()
        if offset is None:
            return None
        total = int(offset.total_seconds()) // 60
        sign = "+" if total >= 0 else "-"
        hours, minutes = divmod(abs(total), 60)
        return f"UTC{sign}{hours:02d}:{minutes:02d}"
    except Exception:
        return None


def _first_valid(values: list[Any], validator: Any) -> tuple[str | None, str | None]:
    """(preferred, secondary): the first valid value becomes preferred (a
    missing/invalid primary never makes the whole line disappear); a later
    distinct valid value becomes the secondary."""
    valid = []
    for raw in values:
        v = validator(raw)
        if v is not None and v not in valid:
            valid.append(v)
        elif v is None and str(raw or "").strip():
            logger.warning("formatting guide: unusable profile value %r omitted", raw)
    preferred = valid[0] if valid else None
    secondary = valid[1] if len(valid) > 1 else None
    return preferred, secondary


def valid_profile_languages(profile: dict[str, Any]) -> tuple[str | None, str | None]:
    """The first two declared languages through the shared prompt boundary.

    A ``prefer`` row sorts first; declaration order settles the remainder.
    """
    data = (profile or {}).get("data") or {}
    rows = effective_language_rows(data)
    ordered = sorted(
        enumerate(rows),
        key=lambda item: (0 if item[1].get("stance") == "prefer" else 1,
                          item[0]))
    return _first_valid(
        [row.get("tag") for _, row in ordered], _valid_language)


# ---- the renderer --------------------------------------------------------

@dataclass(frozen=True)
class FormattingGuide:
    """The guide as the identity block renders it: one comment per profile
    field it can say something about (registry key -> sentence, no leading
    `#`). Language is not here: the reply language is the response-language
    classifier's decision (reply_language_markdown), and the language rows
    have no field in the block to sit on. Empty when no directive is
    usable."""

    comments: dict[str, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.comments)

    @property
    def chars(self) -> int:
        """The characters the guide adds to the prompt — what the shared
        guidance budget deducts before the calibration block takes the
        remainder."""
        return sum(map(len, self.comments.values()))


def _sentence(text: str) -> str:
    """A comment starts with a capital and ends with a period, whatever
    shape the lookup entry has (the entries read as clauses so they can be
    joined). Whitespace collapses to single spaces: a comment is one line by
    construction, and a newline would end it early."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else text + "."


def format_formatting_guide(profile: dict[str, Any],
                            now: datetime | None = None) -> FormattingGuide:
    """Render one profile's locale fields as the guide's comments
    (deterministic; no DB access). `now` is the injectable clock for the
    timezone offset; tests pin it on both sides of a DST boundary."""
    data = profile.get("data") or {}
    if now is None:
        now = datetime.now(UTC)
    comments: dict[str, str] = {}

    date_entry = DATE_FORMATS.get(str(data.get("date_format") or "").strip())
    if date_entry is not None:
        example, warning = date_entry
        comments["date_format"] = _sentence(
            f"for example {example}; {warning}")

    week = WEEK_STARTS.get(str(data.get("first_day_of_week") or "").strip())
    if week is not None:
        comments["first_day_of_week"] = _sentence(week)

    clock = TIME_FORMATS.get(str(data.get("time_format") or "").strip())
    if clock is not None:
        comments["time_format"] = _sentence(clock)

    zone = _valid_timezone(data.get("timezone"))
    if data.get("timezone") and zone is None:
        logger.warning("formatting guide: unusable timezone %r omitted",
                       data.get("timezone"))
    if zone is not None:
        offset = _utc_offset(zone, now)
        where = f"{zone} (currently {offset})" if offset else zone
        comments["timezone"] = _sentence(
            f"present local times in {where}; name another zone when "
            "relevant")

    units_value = str(data.get("units") or "").strip()
    units = UNITS.get(units_value)
    temperature_value = str(data.get("temperature") or "").strip()
    derived_temperature = (
        None if temperature_value
        else TEMPERATURES.get(_UNITS_DEFAULT_TEMPERATURE.get(units_value, "")))
    if units is not None:
        # An unset temperature field has no line of its own to carry the
        # units-derived default, so it rides the units comment.
        derived = (f" Temperature in {derived_temperature}."
                   if derived_temperature else "")
        comments["units"] = _sentence(
            f"{units}; preserve a source value when precision matters and "
            f"add the conversion.{derived}")

    temperature = TEMPERATURES.get(temperature_value)
    if temperature is not None:
        comments["temperature"] = _sentence(temperature)

    currency_examples = NUMBER_FORMATS.get(
        str(data.get("number_format") or "").strip())
    # The first valid code is the primary whichever field holds it (a
    # missing/invalid primary never loses the whole currency comment); a
    # later distinct valid code is the secondary. Each comment attaches to
    # the field whose value it explains.
    valid_codes: list[tuple[str, str]] = []
    for key in ("currency", "currency_2"):
        code = _valid_currency(data.get(key))
        if code is None:
            if str(data.get(key) or "").strip():
                logger.warning(
                    "formatting guide: unusable profile value %r omitted",
                    data.get(key))
        elif code not in (c for _, c in valid_codes):
            valid_codes.append((key, code))
    if valid_codes:
        primary_key, primary = valid_codes[0]
        convert = ("Convert currencies only with a supplied or freshly "
                   "retrieved rate.")
        if currency_examples is not None:
            digits = (0 if primary in ZERO_DECIMAL_CURRENCIES_V1
                      else 3 if primary in THREE_DECIMAL_CURRENCIES_V1 else 2)
            comments[primary_key] = _sentence(
                f"for example {currency_examples[digits]} {primary}. "
                f"{convert}")
        else:
            # Without a usable number_format the comment states the
            # conversion rule without inventing separators.
            comments[primary_key] = _sentence(convert)
        if len(valid_codes) > 1:
            comments[valid_codes[1][0]] = _sentence(
                "a secondary option when the task already involves it")

    guide = FormattingGuide(comments=comments)
    if guide.chars > MAX_FORMATTING_GUIDE_CHARS:
        raise ValueError(
            f"formatting guide exceeds {MAX_FORMATTING_GUIDE_CHARS} chars "
            f"({guide.chars}) — a lookup entry grew past the budget")
    return guide
