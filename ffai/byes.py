import json
from pathlib import Path

DEFAULT_BYE_WEEKS_PATH = Path(__file__).resolve().parent.parent / "data" / "bye_weeks_2026.json"


def load_bye_weeks(path=DEFAULT_BYE_WEEKS_PATH):
    """Static team -> bye_week lookup, generated once via nflreadpy (dev-only
    dependency -- no network call or heavy dependency in the runtime/draft-day
    path, R5-style isolation of a data source that's fixed for the season).
    A missing file returns {} rather than raising: bye weeks are a reasoning
    signal, not load-bearing data (PRD R2)."""
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}
