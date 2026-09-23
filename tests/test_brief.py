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

    assert "**Projected total: 35.0 pts**" in text
    assert "| **QB** | QB-1" in text
    assert "| **RB** | RB-2" in text


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

    assert "Changes vs. your currently-set lineup" in text
    assert "| **Start** | QB-1 (QB) -- 20.0 pts |" in text
    assert "| **Sit** | QB-2 (QB) -- 5.0 pts |" in text


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


# --- action items ----------------------------------------------------------
# Regression cover for the Week 3 2026 brief, which rendered a ruled-out QB
# projected 0.0 in the starting lineup and drew no conclusion from it.

def _waiver_plan(targets=(), bids=None, faab=True, remaining=100, pace=None, priority=None, degraded=False):
    from ffai.waivers import WaiverPlan

    targets = list(targets)
    return WaiverPlan(
        targets=targets, ranked=targets, bids=bids or {}, label="FAAB Bidding", faab=faab,
        remaining_budget=remaining, pace_warning=pace, priority_note=priority, degraded=degraded,
    )


def _target(player, marginal_value=10.0, trending=None):
    from ffai.models import WaiverTarget

    return WaiverTarget(player=player, marginal_value=marginal_value, trending_count=trending, reasons=[])


def test_brief_calls_out_a_ruled_out_starter_as_an_action_item():
    context = _base_context(
        roster_positions=["QB"],
        my_players=[_player("1", "QB", 0.0)],
        my_roster={"roster_id": 1, "starters": ["1"]},
        players_raw={"1": {"injury_status": "Out"}},
    )

    text = render_brief_markdown(context)

    assert "Action Required" in text
    assert "lineup hole" in text
    assert "**Out**" in text


def test_brief_names_the_replacement_and_the_bid():
    context = _base_context(
        roster_positions=["QB"],
        my_players=[_player("1", "QB", 0.0)],
        my_roster={"roster_id": 1, "starters": ["1"]},
        players_raw={"1": {"injury_status": "Out"}},
    )
    plan = _waiver_plan(targets=[_target(_player("99", "QB", 17.6), marginal_value=17.6)], bids={"99": (20, 35)})

    text = render_brief_markdown(context, waiver_plan=plan)

    assert "QB-99" in text
    assert "**$20-$35**" in text
    assert "**+17.6 pts**" in text


def test_brief_says_hold_when_nothing_on_the_wire_is_an_upgrade():
    context = _base_context(
        roster_positions=["QB"],
        my_players=[_player("1", "QB", 2.0)],
        my_roster={"roster_id": 1, "starters": ["1"]},
    )

    text = render_brief_markdown(context, waiver_plan=_waiver_plan(targets=[_target(_player("99", "QB", 2.2))]))

    assert "Hold." in text


def test_brief_reports_a_clean_lineup_when_there_are_no_holes():
    text = render_brief_markdown(_base_context())

    assert "No lineup holes." in text
    assert "Action Required" not in text


def test_brief_renders_waiver_targets_grouped_by_position():
    plan = _waiver_plan(
        targets=[_target(_player("99", "QB", 17.6), 17.6), _target(_player("98", "RB", 11.0), 4.0)],
        bids={"99": (20, 35)},
    )

    text = render_brief_markdown(_base_context(), waiver_plan=plan)

    assert "## Waiver Wire" in text
    assert "**Budget remaining: $100**" in text
    assert "**QB**" in text and "**RB**" in text


def test_brief_shows_projected_margin_against_the_opponent():
    context = _base_context(
        opponent_roster={"roster_id": 2, "starters": ["3"]},
        opponent_display_name="Rival",
        opponent_players=[_player("3", "WR", 50.0, team="SF")],
    )

    text = render_brief_markdown(context)

    assert "projected to **lose by 15.0**" in text


def test_brief_omits_action_section_when_projections_are_degraded():
    # With no projections every slot would look like a hole -- an action list
    # full of false alarms is worse than none (PRD: confidently wrong).
    context = _base_context(projections_degraded=True, my_roster={"roster_id": 1, "starters": ["1"]})

    text = render_brief_markdown(context)

    assert "Action Required" not in text
    assert "NO PROJECTIONS AVAILABLE" in text
