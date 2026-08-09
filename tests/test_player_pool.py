from ffai.player_pool import build_projection, is_draftable, to_projection_pool
from ffai.projections import ProjectionsResult


def _player(position="RB", team="DET", active=True, full_name="Test Player"):
    return {"position": position, "team": team, "active": active, "full_name": full_name}


def test_is_draftable_requires_position_active_and_team():
    assert is_draftable(_player()) is True
    assert is_draftable(_player(position="LB")) is False
    assert is_draftable(_player(active=False)) is False
    assert is_draftable(_player(team=None)) is False


def test_to_projection_pool_scores_players_with_matching_projection():
    players_raw = {"1": _player()}
    projections = ProjectionsResult(
        raw_stats_by_player={"1": {"rush_yd": 100.0}}, source="sleeper", degraded=False,
    )
    scoring_settings = {"rush_yd": 0.1}

    pool = to_projection_pool(players_raw, projections, scoring_settings)

    assert len(pool) == 1
    assert pool[0].points == 10.0
    assert pool[0].data_source == "sleeper"


def test_to_projection_pool_no_projection_entry_defaults_zero_none_source():
    players_raw = {"1": _player()}
    projections = ProjectionsResult(raw_stats_by_player={}, source="sleeper", degraded=False)

    pool = to_projection_pool(players_raw, projections, scoring_settings={"rush_yd": 0.1})

    assert pool[0].points == 0.0
    assert pool[0].data_source == "none"


def test_to_projection_pool_filters_out_of_scope_positions():
    players_raw = {"1": _player(position="LB")}  # IDP, out of PRD scope
    projections = ProjectionsResult(raw_stats_by_player={}, source="sleeper", degraded=False)

    pool = to_projection_pool(players_raw, projections, scoring_settings={})

    assert pool == []


def test_to_projection_pool_filters_inactive_players():
    players_raw = {"1": _player(active=False)}
    projections = ProjectionsResult(raw_stats_by_player={}, source="sleeper", degraded=False)

    pool = to_projection_pool(players_raw, projections, scoring_settings={})

    assert pool == []


def test_to_projection_pool_attaches_bye_week_by_team():
    players_raw = {"1": _player(team="DET")}
    projections = ProjectionsResult(raw_stats_by_player={}, source="sleeper", degraded=False)

    pool = to_projection_pool(players_raw, projections, scoring_settings={}, byes_by_team={"DET": 6})

    assert pool[0].bye_week == 6


def test_to_projection_pool_missing_bye_week_data_defaults_to_none():
    players_raw = {"1": _player(team="DET")}
    projections = ProjectionsResult(raw_stats_by_player={}, source="sleeper", degraded=False)

    pool = to_projection_pool(players_raw, projections, scoring_settings={})

    assert pool[0].bye_week is None


def test_build_projection_ignores_draftable_filter_unlike_pool():
    # A rostered player who's inactive or teamless (e.g. IR, between teams)
    # must still resolve for weekly_context.py -- unlike to_projection_pool,
    # which filters those out of the draft board.
    players_raw = {"1": _player(active=False, team=None)}
    projections = ProjectionsResult(raw_stats_by_player={}, source="sleeper", degraded=False)

    projection = build_projection("1", players_raw, projections, scoring_settings={})

    assert projection is not None
    assert projection.points == 0.0
    assert projection.data_source == "none"


def test_build_projection_returns_none_for_unknown_player_id():
    projections = ProjectionsResult(raw_stats_by_player={}, source="sleeper", degraded=False)

    assert build_projection("missing", {}, projections, scoring_settings={}) is None


def test_to_projection_pool_filters_players_with_no_team():
    # Sleeper keeps thousands of practice-squad/unrostered entries marked
    # active=True with team=None -- not real draftable options.
    players_raw = {"1": _player(team=None)}
    projections = ProjectionsResult(raw_stats_by_player={}, source="sleeper", degraded=False)

    pool = to_projection_pool(players_raw, projections, scoring_settings={})

    assert pool == []
