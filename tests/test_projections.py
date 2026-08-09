import json
from pathlib import Path

from ffai.projections import (
    ProjectionsAdapter,
    ProjectionsFetchError,
    _match_fantasypros_player,
    _normalize_name,
    _parse_fantasypros_table,
)

FIXTURES = Path(__file__).parent / "fixtures"


class _StubAdapter(ProjectionsAdapter):
    """Overrides the network-touching helpers so fetch()'s fallback logic can
    be tested without any real HTTP calls -- same "plain fake" convention as
    FakeClient in test_repository.py, just via subclassing since these are
    instance methods on the adapter itself rather than an injected client."""

    def __init__(self, sleeper_raw=None, sleeper_raises=False, espn_raw=None, espn_raises=False):
        super().__init__()
        self.sleeper_raw = sleeper_raw
        self.sleeper_raises = sleeper_raises
        self.espn_raw = espn_raw
        self.espn_raises = espn_raises

    def _fetch_sleeper(self, season, week, positions):
        if self.sleeper_raises:
            raise ProjectionsFetchError("simulated sleeper failure")
        return self.sleeper_raw

    def _fetch_espn(self, players_raw, season, week, positions):
        if self.espn_raises:
            raise ProjectionsFetchError("simulated espn failure")
        return self.espn_raw


def _good_payload(n=100):
    return {str(i): {"rush_yd": 500.0, "rush_td": 5.0} for i in range(n)}


def test_fetch_returns_sleeper_source_on_success():
    adapter = _StubAdapter(sleeper_raw=_good_payload())

    result = adapter.fetch(players_raw={}, season="2026")

    assert result.source == "sleeper"
    assert result.degraded is False
    assert len(result.raw_stats_by_player) == 100


def test_fetch_falls_back_to_espn_when_sleeper_fails():
    adapter = _StubAdapter(sleeper_raises=True, espn_raw=_good_payload())

    result = adapter.fetch(players_raw={}, season="2026")

    assert result.source == "espn"
    assert result.degraded is False


def test_fetch_falls_back_to_espn_when_sleeper_fails_validation():
    too_small = {"1": {"rush_yd": 10.0}}  # below MIN_PAYLOAD_SIZE
    adapter = _StubAdapter(sleeper_raw=too_small, espn_raw=_good_payload())

    result = adapter.fetch(players_raw={}, season="2026")

    assert result.source == "espn"


def test_fetch_degraded_when_both_sources_fail():
    adapter = _StubAdapter(sleeper_raises=True, espn_raises=True)

    result = adapter.fetch(players_raw={}, season="2026")

    assert result.source == "none"
    assert result.degraded is True
    assert result.raw_stats_by_player == {}
    assert result.warnings


def test_fetch_espn_refuses_weekly_fetch_rather_than_guess():
    adapter = ProjectionsAdapter()

    try:
        adapter._fetch_espn(players_raw={}, season="2026", week=5, positions=["RB"])
        assert False, "expected ProjectionsFetchError for a weekly ESPN fetch"
    except ProjectionsFetchError:
        pass


def test_fetch_degrades_for_a_week_when_sleeper_fails(monkeypatch):
    # ESPN is season-only (see above) -- a weekly fetch with a failing
    # Sleeper source has no real fallback and must degrade honestly rather
    # than fall through to ESPN's season data. Uses the real (unstubbed)
    # _fetch_espn so its week-refusal actually runs.
    def _raise_sleeper_failure(season, week, positions):
        raise ProjectionsFetchError("simulated sleeper failure")

    adapter = ProjectionsAdapter()
    monkeypatch.setattr(adapter, "_fetch_sleeper", _raise_sleeper_failure)

    result = adapter.fetch(players_raw={}, season="2026", week=5)

    assert result.source == "none"
    assert result.degraded is True


def test_validate_rejects_payload_below_min_size():
    adapter = ProjectionsAdapter()

    ok, warnings = adapter._validate({"1": {"rush_yd": 100.0}}, known_player_ids=None)

    assert ok is False
    assert warnings


def test_validate_rejects_implausible_stat_values():
    adapter = ProjectionsAdapter()
    raw = _good_payload()
    # a single wild outlier shouldn't fail the whole payload (real superstar
    # seasons happen) -- push enough entries past the bound to cross the
    # MAX_BOUND_VIOLATION_FRACTION threshold instead.
    for i in range(20):
        raw[str(i)]["rush_yd"] = 99999.0

    ok, warnings = adapter._validate(raw, known_player_ids=None)

    assert ok is False


def test_validate_rejects_low_coverage_of_known_players():
    adapter = ProjectionsAdapter()
    raw = _good_payload(n=100)
    known_ids = {str(i) for i in range(100)} | {f"missing{i}" for i in range(50)}  # 50/150 = 33% coverage

    ok, warnings = adapter._validate(raw, known_player_ids=known_ids)

    assert ok is False


def test_validate_passes_good_payload():
    adapter = ProjectionsAdapter()
    raw = _good_payload(n=100)
    known_ids = {str(i) for i in range(100)}

    ok, warnings = adapter._validate(raw, known_player_ids=known_ids)

    assert ok is True
    assert warnings == []


def test_normalize_name_strips_suffix_and_punctuation():
    assert _normalize_name("Marvin Harrison Jr.") == "marvinharrison"
    assert _normalize_name("Odell Beckham II") == "odellbeckham"
    assert _normalize_name("D'Andre Swift") == "dandreswift"


def test_parse_fantasypros_table_real_fixture():
    html = (FIXTURES / "fantasypros_rb_draft_table.html").read_text(encoding="utf-8")

    entries = _parse_fantasypros_table(html, "RB")

    assert len(entries) == 10
    top = entries[0]
    assert top.name == "Jahmyr Gibbs"
    assert top.team == "DET"
    assert top.position == "RB"
    assert top.raw_stats["rush_att"] == 274.4
    assert top.raw_stats["rush_yd"] == 1381.4
    assert top.raw_stats["rush_td"] == 13.8
    assert top.raw_stats["rec"] == 70.9
    assert top.raw_stats["rec_yd"] == 580.6
    assert top.raw_stats["rec_td"] == 4.1
    assert top.raw_stats["fum_lost"] == 1.1
    assert "fpts" not in {k.lower() for k in top.raw_stats}  # FPTS deliberately not carried through


def test_match_fantasypros_player_against_real_players_fixture():
    players_raw = json.loads((FIXTURES / "sleeper_players_sample.json").read_text())

    player_id = _match_fantasypros_player("Jahmyr Gibbs", "DET", "RB", players_raw)

    assert player_id == "9221"


def test_match_fantasypros_player_no_match_returns_none():
    players_raw = json.loads((FIXTURES / "sleeper_players_sample.json").read_text())

    assert _match_fantasypros_player("Nobody Real", "ZZZ", "RB", players_raw) is None


def test_fetch_consensus_top10_matches_real_fixture_players(monkeypatch):
    players_raw = json.loads((FIXTURES / "sleeper_players_sample.json").read_text())
    rb_html = (FIXTURES / "fantasypros_rb_draft_table.html").read_text(encoding="utf-8")

    adapter = ProjectionsAdapter()
    monkeypatch.setattr(adapter, "_fetch_fantasypros", lambda position: _parse_fantasypros_table(rb_html, position))

    result = adapter.fetch_consensus_top10(players_raw, positions=["RB"])

    names = {e.name for e in result["RB"]}
    assert "Jahmyr Gibbs" in names
    assert "Bijan Robinson" in names
    assert "Christian McCaffrey" in names
    assert "Jonathan Taylor" in names
    # every returned entry matched to a real player_id (unmatched entries -- e.g.
    # players not in this trimmed fixture -- are dropped, not guessed)
    assert all(e.player_id is not None for e in result["RB"])
