from ffai.brief import render_brief_markdown
from ffai.models import PlayerProjection
from ffai.weekly_context import WeeklyContext


def _player(player_id, position, points, team="KC"):
    return PlayerProjection(
        player_id=player_id, name=f"{position}-{player_id}", position=position, team=team,
        bye_week=None, points=points, data_source="test",
    )


def _base_context(**overrides):
    defaults = dict(
        week=1,
        season="2026",
        league={},
        players_raw={},
        roster_positions=["QB", "RB", "BN"],
        my_roster={"roster_id": 1, "starters": ["0", "0"]},
        my_players=[_player("1", "QB", 20.0), _player("2", "RB", 15.0)],
        opponent_roster=None,
        opponent_display_name=None,
        opponent_players=[],
        projections_degraded=False,
        warnings=[],
    )
    defaults.update(overrides)
    return WeeklyContext(**defaults)


def test_render_brief_includes_heartbeat_line():
    text = render_brief_markdown(_base_context())

    assert "Last successful run:" in text


def test_render_brief_shows_staleness_banner_when_warnings_present():
    text = render_brief_markdown(_base_context(warnings=["league data is stale -- 3.0 hours old (cached x)"]))

    assert "DO NOT FULLY TRUST THIS BRIEF" in text
    assert "league data is stale -- 3.0 hours old" in text


def test_render_brief_omits_banner_when_no_warnings():
    text = render_brief_markdown(_base_context(warnings=[]))

    assert "DO NOT FULLY TRUST THIS BRIEF" not in text


def test_render_brief_shows_optimal_lineup_and_bench():
    text = render_brief_markdown(_base_context())

    assert "Recommended lineup (35.0 pts):" in text
    assert "QB: QB-1" in text
    assert "RB: RB-2" in text


def test_render_brief_no_roster_found_message():
    text = render_brief_markdown(_base_context(my_roster=None, my_players=[]))

    assert "No roster found for you in this league yet." in text


def test_render_brief_degraded_shows_current_starters_not_optimized(monkeypatch):
    context = _base_context(
        projections_degraded=True,
        my_roster={"roster_id": 1, "starters": ["1"]},
    )

    text = render_brief_markdown(context)

    assert "NO PROJECTIONS AVAILABLE" in text
    assert "QB-1" in text


def test_render_brief_flags_injury_status():
    context = _base_context(players_raw={"1": {"injury_status": "Questionable"}})

    text = render_brief_markdown(context)

    assert "[Questionable]" in text


def test_render_brief_shows_lineup_changes_vs_current_starters():
    # Only one QB slot, two QB-eligible players -- the optimal lineup starts
    # the higher-scorer (QB-1, 20 pts) while the currently-set lineup has
    # the lower one (QB-2, 5 pts) started instead.
    context = _base_context(
        roster_positions=["QB", "BN"],
        my_players=[_player("1", "QB", 20.0), _player("2", "QB", 5.0)],
        my_roster={"roster_id": 1, "starters": ["2"]},
    )

    text = render_brief_markdown(context)

    assert "Changes vs. your currently-set lineup:" in text
    assert "Start QB-1" in text
    assert "Sit QB-2" in text


def test_render_brief_opponent_section_no_matchup():
    text = render_brief_markdown(_base_context(opponent_roster=None))

    assert "No matchup data available yet." in text


def test_render_brief_opponent_section_shows_their_starters():
    context = _base_context(
        opponent_roster={"roster_id": 2, "starters": ["3"]},
        opponent_display_name="Rival",
        opponent_players=[_player("3", "WR", 12.0, team="SF")],
    )

    text = render_brief_markdown(context)

    assert "Facing **Rival**." in text
    assert "WR-3" in text
