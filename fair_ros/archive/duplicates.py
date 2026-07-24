"""Detect a near-duplicate of a mission about to be saved.

Catches the field mistake where an operator re-briefs and saves the same outing
twice, often with a typo in the place name (e.g. "Crosslab" vs "Crossloab"). We
look in the index for a recently-saved mission by the same operator at a very
similar location and surface it at ``mission_close`` so the operator can notice
before saving a confusing duplicate. It never blocks — repeat missions at one
place are legitimate.
"""

import difflib
from datetime import datetime, timedelta

from fair_ros.archive import index
from fair_ros.manifest.schema import MissionRecord
from fair_ros.utils.topic_health import humanize_duration

# A location this close (0..1) counts as "the same place, maybe mistyped".
# "crosslab"/"crossloab" ≈ 0.94; unrelated names fall well below.
LOCATION_SIMILARITY = 0.85
DEFAULT_WINDOW = timedelta(hours=24)


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def find_similar(record: MissionRecord,
                 window: timedelta = DEFAULT_WINDOW) -> list[dict]:
    """Saved missions that look like duplicates of ``record``, newest first.

    Criteria: same operator, saved within ``window`` of this mission's time, and
    a location string at least ``LOCATION_SIMILARITY`` similar. Index errors
    degrade to an empty list — this is a courtesy check, never fatal.
    """
    try:
        rows, _ = index.query(operator=record.identity.operator_name, limit=50)
    except Exception:
        return []

    new_loc = _norm(record.intent.location_name)
    new_when = record.identity.created_at
    matches = []
    for row in rows:
        if row["mission_id"] == record.identity.mission_id:
            continue
        if _norm(row["operator"]) != _norm(record.identity.operator_name):
            continue
        try:
            when = datetime.fromisoformat(row["created_at"])
        except (ValueError, TypeError):
            continue
        if abs((new_when - when).total_seconds()) > window.total_seconds():
            continue
        ratio = difflib.SequenceMatcher(
            None, new_loc, _norm(row["location"])).ratio()
        if ratio >= LOCATION_SIMILARITY:
            matches.append(row)
    return matches


def describe(record: MissionRecord, row: dict) -> str:
    """A plain-language one-liner about a likely-duplicate saved mission."""
    try:
        when = datetime.fromisoformat(row["created_at"])
        elapsed = (record.identity.created_at - when).total_seconds()
        ago = f"{humanize_duration(elapsed)} ago"
    except (ValueError, TypeError):
        ago = "earlier"
    return (f'You already saved a mission {ago} at "{row["location"]}" '
            f'("{row["goal"]}"). Save this only if it really is a different '
            "mission — otherwise you may be duplicating it (check for a typo "
            "in the place name).")
