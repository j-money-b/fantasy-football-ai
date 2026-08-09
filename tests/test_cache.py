from ffai.cache import Cache


def test_set_then_get_round_trips(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))

    cache.set("league:123", {"name": "test league", "season": "2026"})
    result = cache.get("league:123")

    assert result is not None
    payload, fetched_at = result
    assert payload == {"name": "test league", "season": "2026"}
    assert fetched_at


def test_get_missing_key_returns_none(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))

    assert cache.get("league:does-not-exist") is None


def test_set_overwrites_existing_key(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))

    cache.set("league:123", {"name": "old"})
    cache.set("league:123", {"name": "new"})
    payload, _ = cache.get("league:123")

    assert payload == {"name": "new"}
