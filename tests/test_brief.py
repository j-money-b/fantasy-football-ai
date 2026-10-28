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

    assert "**Best lineup: 35.0 pts**" in text
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

    assert "**Start QB-1** (QB), **bench QB-2** -- QB-1 projects 20.0 vs. 5.0 for QB-2." in text


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


# --- the Week 8 2026 failure ----------------------------------------------
# Sleeper never zeroes a ruled-out player's projection, so the old brief told
# the user to "hold" two Out starters and never suggested a bench swap or a
# pickup. These cover the replacement behaviour end to end through the
# renderer.

def _out(player_id, position, points, status="Out", body_part="Concussion"):
    from ffai.availability import apply_availability

    raw = {player_id: {"injury_status": status, "injury_body_part": body_part}}
    return apply_availability([_player(player_id, position, points)], raw, week=1)[0], raw


def test_brief_benches_a_ruled_out_starter_for_a_healthy_bench_player():
    out, raw = _out("1", "WR", 11.7)
    context = _base_context(
        roster_positions=["WR", "BN"],
        my_players=[out, _player("2", "WR", 10.3)],
        my_roster={"roster_id": 1, "starters": ["1"]},
        players_raw=raw,
    )

    text = render_brief_markdown(context)

    assert "**Start WR-2** instead of WR-1 -- WR-1 is Out (Concussion) and won't score" in text
    assert "Hold" not in text


def _move_plan(moves=(), wire=None, rising=(), weeks=(1, 2, 3, 4)):
    from ffai.roster_moves import MovePlan

    return MovePlan(weeks=list(weeks), moves=list(moves), wire=wire or {}, rising=list(rising))


def _waiver_plan(moves=None, faab=True, remaining=100, pace=None):
    from ffai.waivers import WaiverPlan

    return WaiverPlan(
        targets=[], ranked=[], bids={}, label="FAAB Bidding", faab=faab,
        remaining_budget=remaining, pace_warning=pace, priority_note=None, degraded=False,
        moves=moves if moves is not None else _move_plan(),
    )


def _roster_move(add, drop, bid=(6, 12), worth=20):
    from ffai.roster_moves import RosterMove

    return RosterMove(
        add=add, drop=drop, gain=14.9, gain_this_week=1.4, weeks=[1, 2, 3, 4],
        add_by_week=[11.7, 11.1, 12.1, 12.5], drop_by_week=[0.0, 0.0, 0.0, 0.0],
        trending_count=None, bid=bid, worth=worth, market="little competition",
        reasons=["Cut QB-9: he wouldn't start a single week of the next 4."],
    )


def test_brief_names_the_pickup_the_drop_the_bid_and_why():
    move = _roster_move(_player("99", "WR", 11.7), _player("9", "QB", 13.9))
    plan = _waiver_plan(_move_plan([move]))

    text = render_brief_markdown(_base_context(), waiver_plan=plan, now=_tuesday())

    assert "**Add WR-99, drop QB-9** -- bid $6-$12 before Wednesday ~3am ET." in text
    assert "### 1. Add WR-99 (WR, KC) -- drop QB-9 (QB)" in text
    assert "**+14.9 pts** over weeks 1-4" in text
    assert "Cut QB-9: he wouldn't start a single week" in text


def test_brief_says_add_now_once_waivers_have_run():
    move = _roster_move(_player("99", "WR", 11.7), _player("9", "QB", 13.9))
    plan = _waiver_plan(_move_plan([move]))

    text = render_brief_markdown(_base_context(), waiver_plan=plan, now=_saturday())

    assert "add now as a free agent (if he's on waivers, bid $6-$12)" in text


def test_brief_always_shows_the_wire_even_with_nothing_to_do():
    from ffai.roster_moves import WireEntry

    entry = WireEntry(player=_player("98", "RB", 9.0), horizon_points=31.9, gain=0.0, best_drop=None, trending_count=54220)
    plan = _waiver_plan(_move_plan(wire={"RB": [entry]}))

    text = render_brief_markdown(_base_context(), waiver_plan=plan)

    assert "No add/drop is worth making right now" in text
    assert "## On the wire right now" in text
    assert "| RB | RB-98 `KC` | 31.9 | +0.0 | -- | 54,220 |" in text
    assert "**FAAB budget left: $100**" in text


def test_brief_reports_nothing_to_do_when_lineup_is_set_and_wire_is_empty():
    context = _base_context(my_roster={"roster_id": 1, "starters": ["1", "2"]})

    text = render_brief_markdown(context, waiver_plan=_waiver_plan())

    assert "**Nothing to do this week.**" in text


def _tuesday():
    from datetime import datetime
    from ffai.brief_model import EASTERN

    return datetime(2026, 10, 27, 8, 0, tzinfo=EASTERN)


def _saturday():
    from datetime import datetime
    from ffai.brief_model import EASTERN

    return datetime(2026, 10, 31, 8, 0, tzinfo=EASTERN)


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

    assert "Do this" not in text
    assert "NO PROJECTIONS AVAILABLE" in text
