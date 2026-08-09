import pytest

from ffai.cache import Cache
from ffai.projections import ProjectionsResult
from ffai.repository import PLAYERS_CACHE_KEY, fetch_league, fetch_players, fetch_projections
from ffai.sleeper_client import SleeperAPIError


class FakeProjectionsAdapter:
    def __init__(self, result: ProjectionsResult):
        self.result = result
        self.calls = 0

    def fetch(self, players_raw, season, week=None, positions=None, known_player_ids=None):
        self.calls += 1
        return self.result


class FakeClient:
    def __init__(self, league_data=None, players_data=None, raises=False):
        self.league_data = league_data
        self.players_data = players_data
        self.raises = raises
        self.calls = 0

    def get_league(self, league_id):
        self.calls += 1
        if self.raises:
            raise SleeperAPIError("simulated failure")
        return self.league_data

    def get_players(self):
        self.calls += 1
        if self.raises:
            raise SleeperAPIError("simulated failure")
        return self.players_data


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


def test_fetch_players_no_cache_fetches_live_and_caches(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    client = FakeClient(players_data={"1": {"full_name": "Test Player"}})

    data, stale, fetched_at = fetch_players(client, cache)

    assert data == {"1": {"full_name": "Test Player"}}
    assert stale is False
    assert client.calls == 1
    assert cache.get(PLAYERS_CACHE_KEY) == (data, fetched_at)


def test_fetch_players_fresh_cache_skips_live_fetch(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    cache.set(PLAYERS_CACHE_KEY, {"1": {"full_name": "Cached Player"}})
    client = FakeClient(raises=True)  # would raise if called -- must not be called

    data, stale, fetched_at = fetch_players(client, cache, max_age_hours=24)

    assert data == {"1": {"full_name": "Cached Player"}}
    assert stale is False
    assert client.calls == 0


def test_fetch_players_stale_cache_refetches_live(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    cache.set(PLAYERS_CACHE_KEY, {"1": {"full_name": "Old Player"}})
    client = FakeClient(players_data={"1": {"full_name": "Fresh Player"}})

    data, stale, fetched_at = fetch_players(client, cache, max_age_hours=0)

    assert data == {"1": {"full_name": "Fresh Player"}}
    assert stale is False
    assert client.calls == 1


def test_fetch_players_stale_cache_live_failure_falls_back(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    cache.set(PLAYERS_CACHE_KEY, {"1": {"full_name": "Old Player"}})
    failing_client = FakeClient(raises=True)

    data, stale, fetched_at = fetch_players(failing_client, cache, max_age_hours=0)

    assert data == {"1": {"full_name": "Old Player"}}
    assert stale is True


def test_fetch_players_no_cache_live_failure_raises(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    failing_client = FakeClient(raises=True)

    with pytest.raises(SleeperAPIError):
        fetch_players(failing_client, cache)


def test_fetch_projections_success_caches_and_returns(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    good_result = ProjectionsResult(raw_stats_by_player={"1": {"rush_yd": 100.0}}, source="sleeper", degraded=False)
    adapter = FakeProjectionsAdapter(good_result)

    result, stale, fetched_at = fetch_projections(adapter, cache, players_raw={}, season="2026")

    assert result is good_result
    assert stale is False
    assert fetched_at
    assert cache.get("projections:2026:season") is not None


def test_fetch_projections_degraded_falls_back_to_cached_payload(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    good_result = ProjectionsResult(raw_stats_by_player={"1": {"rush_yd": 100.0}}, source="sleeper", degraded=False)
    fetch_projections(FakeProjectionsAdapter(good_result), cache, players_raw={}, season="2026")

    degraded_result = ProjectionsResult(raw_stats_by_player={}, source="none", degraded=True, warnings=["boom"])
    result, stale, fetched_at = fetch_projections(FakeProjectionsAdapter(degraded_result), cache, players_raw={}, season="2026")

    assert result.raw_stats_by_player == {"1": {"rush_yd": 100.0}}
    assert result.source == "sleeper"
    assert result.degraded is False
    assert stale is True
    assert fetched_at


def test_fetch_projections_degraded_no_cache_returns_degraded_result(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    degraded_result = ProjectionsResult(raw_stats_by_player={}, source="none", degraded=True, warnings=["boom"])

    result, stale, fetched_at = fetch_projections(FakeProjectionsAdapter(degraded_result), cache, players_raw={}, season="2026")

    assert result is degraded_result
    assert stale is True
    assert fetched_at is None
