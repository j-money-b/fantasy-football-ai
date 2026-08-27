import pytest

from ffai import adp as adp_module
from ffai.adp import AdpFetchError, _sleeper_id, fetch_adp
from ffai.cache import Cache


def _players_raw():
    return {
        "1001": {
            "position": "RB", "team": "DET", "full_name": "Jahmyr Gibbs",
            "search_full_name": "jahmyrgibbs", "active": True,
        },
        "1002": {
            "position": "K", "team": "SF", "full_name": "Eddy Pineiro",
            "search_full_name": "eddypineiro", "active": True,
        },
        "SEA": {
            "position": "DEF", "team": "SEA", "full_name": "Seattle Seahawks",
            "search_full_name": "seattleseahawks", "active": True,
        },
    }


def _entry(name, position, team, adp):
    return {"name": name, "position": position, "team": team, "adp": adp}


def _payload(entries, total_drafts=7830):
    return {
        "players": entries,
        "meta": {"type": "PPR", "teams": 10, "total_drafts": total_drafts,
                 "start_date": "2026-08-19", "end_date": "2026-08-26"},
    }


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _stub_requests(monkeypatch, payload):
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append((url, params))
        return _FakeResponse(payload)

    monkeypatch.setattr(adp_module.requests, "get", fake_get)
    return calls


def _cache(tmp_path):
    return Cache(db_path=str(tmp_path / "cache.sqlite3"))


def test_matches_a_plain_skill_player():
    assert _sleeper_id(_entry("Jahmyr Gibbs", "RB", "DET", 1.5), _players_raw()) == "1001"


def test_kicker_position_alias_pk_to_k():
    """FFC labels kickers PK; Sleeper calls them K. Without the alias the
    position filter in the name matcher rejects every kicker."""
    assert _sleeper_id(_entry("Eddy Pineiro", "PK", "SF", 164.2), _players_raw()) == "1002"


def test_accented_name_folds_to_sleepers_transliteration():
    """The real feed spells him Piñeiro. Stripping punctuation without
    folding accents first turns that into "pieiro" and matches nothing."""
    assert _sleeper_id(_entry("Eddy Piñeiro", "PK", "SF", 164.2), _players_raw()) == "1002"


def test_defense_matches_on_team_code_not_name():
    """FFC says "Seattle Defense"; Sleeper keys defenses by bare team
    abbreviation with a full_name of "Seattle Seahawks". No name
    normalisation bridges that, so the team code is the join."""
    assert _sleeper_id(_entry("Seattle Defense", "DEF", "SEA", 82.6), _players_raw()) == "SEA"


def test_unknown_player_returns_none_rather_than_guessing():
    assert _sleeper_id(_entry("Nobody At All", "WR", "XXX", 200.0), _players_raw()) is None


def test_returns_ids_ordered_by_adp(monkeypatch, tmp_path):
    _stub_requests(monkeypatch, _payload([
        _entry("Seattle Defense", "DEF", "SEA", 82.6),
        _entry("Jahmyr Gibbs", "RB", "DET", 1.5),
        _entry("Eddy Piñeiro", "PK", "SF", 164.2),
    ]))
    monkeypatch.setattr(adp_module, "MIN_PLAYERS", 1)

    ids, stale, meta = fetch_adp(_cache(tmp_path), _players_raw(), teams=10, season="2026")

    assert ids == ["1001", "SEA", "1002"]
    assert stale is False
    assert meta["total_drafts"] == 7830


def test_thin_sample_is_rejected(monkeypatch, tmp_path):
    """A handful of drafts is noise wearing consensus' clothes -- refuse it
    rather than let it silently become the opponent model."""
    _stub_requests(monkeypatch, _payload([_entry("Jahmyr Gibbs", "RB", "DET", 1.5)], total_drafts=3))

    with pytest.raises(AdpFetchError, match="3 drafts"):
        fetch_adp(_cache(tmp_path), _players_raw(), teams=10, season="2026")


def test_mostly_unmatched_feed_is_rejected(monkeypatch, tmp_path):
    """Guards a schema change at the far end: if FFC renames its fields the
    entries still arrive, they just stop matching, and an almost-empty
    order would quietly become a board sorted by nothing."""
    _stub_requests(monkeypatch, _payload([_entry("Nobody At All", "WR", "XXX", 1.0)]))

    with pytest.raises(AdpFetchError, match="matched a Sleeper player"):
        fetch_adp(_cache(tmp_path), _players_raw(), teams=10, season="2026")


def test_fresh_cache_is_served_without_refetching(monkeypatch, tmp_path):
    calls = _stub_requests(monkeypatch, _payload([_entry("Jahmyr Gibbs", "RB", "DET", 1.5)]))
    monkeypatch.setattr(adp_module, "MIN_PLAYERS", 1)
    cache = _cache(tmp_path)

    fetch_adp(cache, _players_raw(), teams=10, season="2026")
    ids, stale, _meta = fetch_adp(cache, _players_raw(), teams=10, season="2026")

    assert len(calls) == 1
    assert ids == ["1001"]
    assert stale is False


def test_live_failure_falls_back_to_stale_cache(monkeypatch, tmp_path):
    _stub_requests(monkeypatch, _payload([_entry("Jahmyr Gibbs", "RB", "DET", 1.5)]))
    monkeypatch.setattr(adp_module, "MIN_PLAYERS", 1)
    cache = _cache(tmp_path)
    fetch_adp(cache, _players_raw(), teams=10, season="2026")

    def boom(*args, **kwargs):
        raise adp_module.requests.RequestException("network down")

    monkeypatch.setattr(adp_module.requests, "get", boom)

    ids, stale, _meta = fetch_adp(cache, _players_raw(), teams=10, season="2026", max_age_hours=0)

    assert ids == ["1001"]
    assert stale is True


def test_live_failure_with_nothing_cached_raises(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise adp_module.requests.RequestException("network down")

    monkeypatch.setattr(adp_module.requests, "get", boom)

    with pytest.raises(AdpFetchError):
        fetch_adp(_cache(tmp_path), _players_raw(), teams=10, season="2026")


def test_cache_key_separates_league_shapes(monkeypatch, tmp_path):
    """ADP is shape-dependent -- a 12-team board does not deplete like a
    10-team one -- so the two must not share a cache entry."""
    calls = _stub_requests(monkeypatch, _payload([_entry("Jahmyr Gibbs", "RB", "DET", 1.5)]))
    monkeypatch.setattr(adp_module, "MIN_PLAYERS", 1)
    cache = _cache(tmp_path)

    fetch_adp(cache, _players_raw(), teams=10, season="2026")
    fetch_adp(cache, _players_raw(), teams=12, season="2026")

    assert [params["teams"] for _url, params in calls] == [10, 12]
