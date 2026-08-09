import pytest

from ffai.cache import Cache
from ffai.repository import fetch_league
from ffai.sleeper_client import SleeperAPIError


class FakeClient:
    def __init__(self, league_data=None, raises=False):
        self.league_data = league_data
        self.raises = raises
        self.calls = 0

    def get_league(self, league_id):
        self.calls += 1
        if self.raises:
            raise SleeperAPIError("simulated failure")
        return self.league_data


def test_live_success_caches_and_returns_fresh(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    client = FakeClient(league_data={"name": "test league"})

    data, stale, fetched_at = fetch_league(client, cache, "123")

    assert data == {"name": "test league"}
    assert stale is False
    assert fetched_at
    assert cache.get("league:123") == ({"name": "test league"}, fetched_at)


def test_live_failure_falls_back_to_cache(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    cache.set("league:123", {"name": "cached league"})
    failing_client = FakeClient(raises=True)

    data, stale, fetched_at = fetch_league(failing_client, cache, "123")

    assert data == {"name": "cached league"}
    assert stale is True
    assert fetched_at


def test_live_failure_with_no_cache_raises(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    failing_client = FakeClient(raises=True)

    with pytest.raises(SleeperAPIError):
        fetch_league(failing_client, cache, "123")
