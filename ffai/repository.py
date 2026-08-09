from datetime import datetime, timedelta, timezone

from ffai.cache import Cache
from ffai.config import PLAYER_DICT_MAX_AGE_HOURS
from ffai.projections import DEFAULT_POSITIONS, ProjectionsAdapter, ProjectionsResult
from ffai.sleeper_client import SleeperAPIError, SleeperClient

PLAYERS_CACHE_KEY = "players:nfl"


def fetch_league(client: SleeperClient, cache: Cache, league_id: str):
    """Fetch the league object live; on failure, fall back to the last cached
    copy (PRD R3: last-known-good). Returns (data, stale, fetched_at).
    Raises SleeperAPIError only if the live fetch fails AND nothing is cached."""
    key = f"league:{league_id}"

    try:
        data = client.get_league(league_id)
    except SleeperAPIError:
        cached = cache.get(key)
        if cached is None:
            raise
        payload, fetched_at = cached
        return payload, True, fetched_at

    fetched_at = cache.set(key, data)
    return data, False, fetched_at


def fetch_players(client: SleeperClient, cache: Cache, max_age_hours: int = PLAYER_DICT_MAX_AGE_HOURS):
    """Fetch the full player dictionary under a "serve from cache if still
    fresh" policy (PRD 3.1: pull at most once daily) -- unlike fetch_league,
    this does NOT try live on every call. Only refetches when the cached copy
    is missing or older than max_age_hours; a live failure at that point
    falls back to the (now-stale) cache, same as fetch_league. Returns
    (data, stale, fetched_at). Raises SleeperAPIError only if a live fetch is
    needed and fails AND nothing is cached."""
    cached = cache.get(PLAYERS_CACHE_KEY)
    if cached is not None:
        payload, fetched_at = cached
        age = datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at)
        if age < timedelta(hours=max_age_hours):
            return payload, False, fetched_at

    try:
        data = client.get_players()
    except SleeperAPIError:
        if cached is None:
            raise
        payload, fetched_at = cached
        return payload, True, fetched_at

    fetched_at = cache.set(PLAYERS_CACHE_KEY, data)
    return data, False, fetched_at


def fetch_projections(
    adapter: ProjectionsAdapter,
    cache: Cache,
    players_raw: dict,
    season: str,
    week=None,
    positions=DEFAULT_POSITIONS,
    known_player_ids=None,
):
    """Runs the adapter's own Sleeper->ESPN fallback chain; if that comes
    back fully degraded (source="none"), falls back to the last cached
    known-good projections payload with a staleness warning -- the same
    last-known-good shape as fetch_league, one layer further out (the
    adapter's internal fallback is the "live attempt" here). Returns
    (ProjectionsResult, stale, fetched_at). If nothing is cached either, the
    degraded ProjectionsResult itself is returned (PRD R2: partial output,
    including an empty raw_stats_by_player, beats a crash)."""
    key = f"projections:{season}:{week if week is not None else 'season'}"
    result = adapter.fetch(players_raw, season, week=week, positions=positions, known_player_ids=known_player_ids)

    if not result.degraded:
        fetched_at = cache.set(key, {"raw_stats_by_player": result.raw_stats_by_player, "source": result.source})
        return result, False, fetched_at

    cached = cache.get(key)
    if cached is None:
        return result, True, None

    payload, fetched_at = cached
    stale_result = ProjectionsResult(
        raw_stats_by_player=payload["raw_stats_by_player"],
        source=payload["source"],
        degraded=False,
        warnings=result.warnings + [f"live projections fetch degraded; serving cached data from {fetched_at}"],
    )
    return stale_result, True, fetched_at


def fetch_draft_picks(client: SleeperClient, cache: Cache, draft_id: str):
    """Always attempts a live fetch -- unlike fetch_players, there's no
    "fresh enough" cache window here, since the picks feed changes constantly
    during a live draft. Falls back to the last-known-good cached picks list
    only on a live failure (e.g. a transient network blip mid-poll), same
    shape as fetch_league. Returns (picks, stale, fetched_at). Raises
    SleeperAPIError only if live fails AND nothing is cached (e.g. the very
    first poll of the draft)."""
    key = f"draft_picks:{draft_id}"
    try:
        data = client.get_draft_picks(draft_id)
    except SleeperAPIError:
        cached = cache.get(key)
        if cached is None:
            raise
        payload, fetched_at = cached
        return payload, True, fetched_at

    fetched_at = cache.set(key, data)
    return data, False, fetched_at
