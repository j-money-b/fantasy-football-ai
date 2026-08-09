from ffai.cache import Cache
from ffai.sleeper_client import SleeperAPIError, SleeperClient


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
