from ffai.cache import Cache
from ffai.retrospective import compute_bench_points_lost, compute_season_bench_report, format_bench_report
from ffai.sleeper_client import SleeperAPIError

PLAYERS_RAW = {
    "qb_good": {"position": "QB", "team": "KC", "active": True, "full_name": "Good QB"},
    "qb_bad": {"position": "QB", "team": "SF", "active": True, "full_name": "Bad QB"},
    "wr1": {"position": "WR", "team": "KC", "active": True, "full_name": "WR One"},
}
SCORING_SETTINGS = {"pass_td": 4.0, "pass_yd": 0.04}
ROSTER_POSITIONS = ["QB", "WR", "BN"]


def _matchup(players, starters, points=None):
    m = {"roster_id": 1, "players": players, "starters": starters}
    if points is not None:
        m["points"] = points
    return m


# --- compute_bench_points_lost ------------------------------------------------

def test_compute_bench_points_lost_identifies_a_better_bench_option():
    # Started the bad QB, but the good QB (on the bench that week) scored more.
    matchup = _matchup(players=["qb_good", "qb_bad", "wr1"], starters=["qb_bad", "wr1"])
    actual_stats = {
        "qb_good": {"pass_td": 4, "pass_yd": 300},  # 4*4 + 300*0.04 = 28.0
        "qb_bad": {"pass_td": 1, "pass_yd": 150},   # 4 + 6 = 10.0
        "wr1": {},
    }

    result = compute_bench_points_lost(matchup, week=3, players_raw=PLAYERS_RAW, actual_stats_by_player=actual_stats, scoring_settings=SCORING_SETTINGS, roster_positions=ROSTER_POSITIONS)

    assert result.week == 3
    assert result.actual_points == 10.0
    assert result.optimal_points == 28.0
    assert result.bench_points_lost == 18.0
    assert [p.player_id for p in result.swapped_in] == ["qb_good"]
    assert [p.player_id for p in result.swapped_out] == ["qb_bad"]


def test_compute_bench_points_lost_zero_when_optimal_lineup_was_already_started():
    matchup = _matchup(players=["qb_good", "wr1"], starters=["qb_good", "wr1"])
    actual_stats = {"qb_good": {"pass_td": 4, "pass_yd": 300}, "wr1": {}}

    result = compute_bench_points_lost(matchup, week=1, players_raw=PLAYERS_RAW, actual_stats_by_player=actual_stats, scoring_settings=SCORING_SETTINGS, roster_positions=ROSTER_POSITIONS)

    assert result.bench_points_lost == 0.0
    assert result.swapped_in == []
    assert result.swapped_out == []


def test_compute_bench_points_lost_falls_back_to_summing_starters_when_points_missing():
    # No "points" field on the matchup -- must derive actual_points from starters' scored stats.
    matchup = _matchup(players=["qb_good", "wr1"], starters=["qb_good"], points=None)
    actual_stats = {"qb_good": {"pass_td": 1, "pass_yd": 100}, "wr1": {}}  # qb_good: 4 + 4 = 8.0

    result = compute_bench_points_lost(matchup, week=1, players_raw=PLAYERS_RAW, actual_stats_by_player=actual_stats, scoring_settings=SCORING_SETTINGS, roster_positions=ROSTER_POSITIONS)

    assert result.actual_points == 8.0


def test_compute_bench_points_lost_uses_matchup_points_when_present():
    matchup = _matchup(players=["qb_good", "wr1"], starters=["qb_good"], points=99.9)
    actual_stats = {"qb_good": {"pass_td": 1, "pass_yd": 100}, "wr1": {}}

    result = compute_bench_points_lost(matchup, week=1, players_raw=PLAYERS_RAW, actual_stats_by_player=actual_stats, scoring_settings=SCORING_SETTINGS, roster_positions=ROSTER_POSITIONS)

    assert result.actual_points == 99.9


# --- compute_season_bench_report ------------------------------------------------

class FakeClient:
    def __init__(self, matchups_by_week=None, stats_by_week=None, raise_matchup_weeks=None, raise_stats_weeks=None):
        self.matchups_by_week = matchups_by_week or {}
        self.stats_by_week = stats_by_week or {}
        self.raise_matchup_weeks = raise_matchup_weeks or set()
        self.raise_stats_weeks = raise_stats_weeks or set()

    def get_matchups(self, league_id, week):
        if week in self.raise_matchup_weeks:
            raise SleeperAPIError("no matchup data")
        return self.matchups_by_week.get(week, [])

    def get_stats(self, season, week):
        if week in self.raise_stats_weeks:
            raise SleeperAPIError("no stats yet")
        return self.stats_by_week.get(week, {})


def test_compute_season_bench_report_aggregates_across_weeks(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    matchup = _matchup(players=["qb_good", "qb_bad"], starters=["qb_bad"])
    client = FakeClient(
        matchups_by_week={1: [matchup], 2: [matchup]},
        stats_by_week={
            1: {"qb_good": {"pass_td": 4, "pass_yd": 300}, "qb_bad": {"pass_td": 1, "pass_yd": 150}},
            2: {"qb_good": {"pass_td": 4, "pass_yd": 300}, "qb_bad": {"pass_td": 1, "pass_yd": 150}},
        },
    )

    results, skipped = compute_season_bench_report(
        client, cache, "league1", my_roster_id=1, players_raw=PLAYERS_RAW,
        scoring_settings=SCORING_SETTINGS, roster_positions=ROSTER_POSITIONS, season="2026",
        start_week=1, end_week=2,
    )

    assert [r.week for r in results] == [1, 2]
    assert all(r.bench_points_lost == 18.0 for r in results)
    assert skipped == []


def test_compute_season_bench_report_skips_weeks_with_no_matchup_data(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    client = FakeClient(raise_matchup_weeks={2})

    results, skipped = compute_season_bench_report(
        client, cache, "league1", my_roster_id=1, players_raw=PLAYERS_RAW,
        scoring_settings=SCORING_SETTINGS, roster_positions=ROSTER_POSITIONS, season="2026",
        start_week=1, end_week=2,
    )

    assert results == []
    assert any(week == 1 for week, _ in skipped)
    assert any(week == 2 and "no matchup" in reason for week, reason in skipped)


def test_compute_season_bench_report_skips_weeks_with_no_roster_owned_matchup(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    client = FakeClient(matchups_by_week={1: [{"roster_id": 999, "players": ["x"], "starters": []}]})

    results, skipped = compute_season_bench_report(
        client, cache, "league1", my_roster_id=1, players_raw=PLAYERS_RAW,
        scoring_settings=SCORING_SETTINGS, roster_positions=ROSTER_POSITIONS, season="2026",
        start_week=1, end_week=1,
    )

    assert results == []
    assert skipped == [(1, "no roster/matchup data for this week yet")]


def test_compute_season_bench_report_skips_weeks_with_no_stats_yet(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    matchup = _matchup(players=["qb_good"], starters=["qb_good"])
    client = FakeClient(matchups_by_week={1: [matchup]}, raise_stats_weeks={1})

    results, skipped = compute_season_bench_report(
        client, cache, "league1", my_roster_id=1, players_raw=PLAYERS_RAW,
        scoring_settings=SCORING_SETTINGS, roster_positions=ROSTER_POSITIONS, season="2026",
        start_week=1, end_week=1,
    )

    assert results == []
    assert skipped == [(1, "no final stats available for this week yet")]


# --- format_bench_report ------------------------------------------------

def test_format_bench_report_no_completed_weeks():
    assert "No completed weeks with data yet." in format_bench_report([], [])


def test_format_bench_report_shows_totals_and_swaps(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    matchup = _matchup(players=["qb_good", "qb_bad"], starters=["qb_bad"])
    actual_stats = {"qb_good": {"pass_td": 4, "pass_yd": 300}, "qb_bad": {"pass_td": 1, "pass_yd": 150}}
    result = compute_bench_points_lost(matchup, week=1, players_raw=PLAYERS_RAW, actual_stats_by_player=actual_stats, scoring_settings=SCORING_SETTINGS, roster_positions=ROSTER_POSITIONS)

    text = format_bench_report([result], skipped=[(2, "no matchup data available")])

    assert "Week 1:" in text
    assert "lost 18.0 pts" in text
    assert "should have started: Good QB" in text
    assert "should have benched: Bad QB" in text
    assert "Total: 18.0 pts lost over 1 week(s)" in text
    assert "Skipped: week 2 (no matchup data available)" in text
