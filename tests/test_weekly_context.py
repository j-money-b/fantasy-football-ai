from ffai.cache import Cache
from ffai.projections import ProjectionsResult
from ffai.sleeper_client import SleeperAPIError
from ffai.weekly_context import gather_weekly_context

LEAGUE_DATA = {
    "season": "2026",
    "scoring_settings": {"rec": 1.0, "rec_yd": 0.1},
    "roster_positions": ["QB", "RB", "WR", "FLEX", "BN"],
}
PLAYERS_DATA = {
    "1": {"position": "QB", "team": "KC", "active": True, "full_name": "QB One"},
    "2": {"position": "RB", "team": "KC", "active": True, "full_name": "RB One"},
    "3": {"position": "WR", "team": "SF", "active": True, "full_name": "WR One"},
}
ROSTERS_DATA = [
    {"roster_id": 1, "owner_id": "u1", "players": ["1", "2"]},
    {"roster_id": 2, "owner_id": "u2", "players": ["3"]},
]
USERS_DATA = [
    {"user_id": "u1", "display_name": "Me"},
    {"user_id": "u2", "display_name": "Rival"},
]
USER_DATA = {"user_id": "u1", "username": "kevinkissedpeter"}
MATCHUPS_DATA = [{"roster_id": 1, "matchup_id": 10}, {"roster_id": 2, "matchup_id": 10}]
NFL_STATE_DATA = {"week": 1, "season_type": "regular"}


class FakeProjectionsAdapter:
    def fetch(self, players_raw, season, week=None, positions=None, known_player_ids=None):
        return ProjectionsResult(
            raw_stats_by_player={"1": {"rec": 0}, "2": {"rec": 3}, "3": {"rec": 5, "rec_yd": 40}},
            source="sleeper",
            degraded=False,
        )


class FakeClient:
    def __init__(self, raise_methods=None):
        self.raise_methods = raise_methods or set()

    def _maybe_raise(self, name):
        if name in self.raise_methods:
            raise SleeperAPIError(f"simulated {name} failure")

    def get_league(self, league_id):
        self._maybe_raise("league")
        return LEAGUE_DATA

    def get_players(self):
        self._maybe_raise("players")
        return PLAYERS_DATA

    def get_rosters(self, league_id):
        self._maybe_raise("rosters")
        return ROSTERS_DATA

    def get_users(self, league_id):
        self._maybe_raise("users")
        return USERS_DATA

    def get_user(self, username):
        self._maybe_raise("user")
        return USER_DATA

    def get_matchups(self, league_id, week):
        self._maybe_raise("matchups")
        return MATCHUPS_DATA

    def get_nfl_state(self):
        self._maybe_raise("state")
        return NFL_STATE_DATA


def test_gather_weekly_context_resolves_my_roster_and_opponent(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    client = FakeClient()

    context = gather_weekly_context(client, cache, "league123", adapter=FakeProjectionsAdapter())

    assert context.week == 1
    assert context.season == "2026"
    assert context.my_roster["roster_id"] == 1
    assert {p.player_id for p in context.my_players} == {"1", "2"}
    assert context.opponent_roster["roster_id"] == 2
    assert context.opponent_display_name == "Rival"
    assert {p.player_id for p in context.opponent_players} == {"3"}
    assert context.projections_degraded is False
    assert context.warnings == []


def test_gather_weekly_context_accepts_explicit_week_override(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    client = FakeClient(raise_methods={"state"})  # would blow up if state were fetched

    context = gather_weekly_context(client, cache, "league123", week=7, adapter=FakeProjectionsAdapter())

    assert context.week == 7


def test_gather_weekly_context_degrades_gracefully_with_no_matchup_data(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    client = FakeClient(raise_methods={"matchups"})

    context = gather_weekly_context(client, cache, "league123", adapter=FakeProjectionsAdapter())

    assert context.my_roster["roster_id"] == 1
    assert context.opponent_roster is None
    assert context.opponent_players == []


def test_gather_weekly_context_collects_staleness_warnings(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    # First a clean run to populate the cache...
    gather_weekly_context(FakeClient(), cache, "league123", adapter=FakeProjectionsAdapter())

    # ...then a run where the live roster fetch fails, forcing last-known-good.
    failing_client = FakeClient(raise_methods={"rosters"})
    context = gather_weekly_context(failing_client, cache, "league123", adapter=FakeProjectionsAdapter())

    assert context.my_roster["roster_id"] == 1  # still resolved, from cache
    assert any("rosters is stale" in w for w in context.warnings)


def test_gather_weekly_context_no_roster_for_user_yields_empty_players(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))

    class NoOwnerClient(FakeClient):
        def get_rosters(self, league_id):
            return [{"roster_id": 1, "owner_id": "someone_else", "players": ["1"]}]

    context = gather_weekly_context(NoOwnerClient(), cache, "league123", adapter=FakeProjectionsAdapter())

    assert context.my_roster is None
    assert context.my_players == []
