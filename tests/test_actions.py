from ffai.actions import HOLE_POINT_THRESHOLD, find_lineup_holes, recommend_fixes
from ffai.lineup import optimize_lineup
from ffai.models import PlayerProjection, WaiverTarget


def _player(player_id, position, points, team="KC", bye_week=None):
    return PlayerProjection(
        player_id=player_id, name=f"{position}-{player_id}", position=position, team=team,
        bye_week=bye_week, points=points, data_source="test",
    )


def _target(player, marginal_value=10.0, trending=None):
    return WaiverTarget(player=player, marginal_value=marginal_value, trending_count=trending, reasons=[])


def _lineup(players, roster_positions):
    return optimize_lineup(players, roster_positions)


# --- find_lineup_holes -----------------------------------------------------

def test_ruled_out_starter_is_a_critical_hole_even_with_a_stale_projection():
    # The real Week 3 2026 failure, minus the zeroed projection: a ruled-out
    # player whose projection has NOT yet been zeroed must still be a hole,
    # otherwise the brief quietly starts someone who will not play.
    players = [_player("1", "QB", 18.0)]
    holes = find_lineup_holes(_lineup(players, ["QB"]), {"1": {"injury_status": "Out"}})

    assert len(holes) == 1
    assert holes[0].severity == "critical"
    assert "**Out**" in holes[0].reason


def test_zero_point_ruled_out_starter_is_flagged():
    players = [_player("1", "QB", 0.0)]
    holes = find_lineup_holes(_lineup(players, ["QB"]), {"1": {"injury_status": "Out"}})

    assert [h.slot for h in holes] == ["QB"]


def test_empty_slot_is_a_critical_hole():
    holes = find_lineup_holes(_lineup([], ["QB"]), {})

    assert holes[0].severity == "critical"
    assert "no player in your QB slot" in holes[0].reason


def test_player_on_bye_is_a_hole():
    players = [_player("1", "RB", 12.0, bye_week=7)]
    holes = find_lineup_holes(_lineup(players, ["RB"]), {}, week=7)

    assert holes[0].severity == "critical"
    assert "on bye" in holes[0].reason


def test_low_projection_is_a_weak_hole_not_a_critical_one():
    players = [_player("1", "RB", HOLE_POINT_THRESHOLD - 1)]
    holes = find_lineup_holes(_lineup(players, ["RB"]), {})

    assert holes[0].severity == "weak"


def test_healthy_productive_starter_is_not_a_hole():
    players = [_player("1", "RB", 15.0)]

    assert find_lineup_holes(_lineup(players, ["RB"]), {}) == []


def test_questionable_is_not_treated_as_ruled_out():
    # Questionable players usually play -- flagging every one as a hole would
    # make the action list noise, which is how a brief stops being read.
    players = [_player("1", "RB", 15.0)]
    holes = find_lineup_holes(_lineup(players, ["RB"]), {"1": {"injury_status": "Questionable"}})

    assert holes == []


# --- recommend_fixes -------------------------------------------------------

def test_fix_matches_the_best_eligible_free_agent_to_the_hole():
    players = [_player("1", "QB", 0.0)]
    holes = find_lineup_holes(_lineup(players, ["QB"]), {"1": {"injury_status": "Out"}})
    targets = [_target(_player("99", "QB", 17.6)), _target(_player("98", "RB", 20.0))]

    items = recommend_fixes(holes, targets)

    assert items[0].fix.player.player_id == "99"  # the RB cannot fill a QB slot
    assert items[0].gain == 17.6


def test_flex_hole_accepts_any_flex_eligible_position():
    players = [_player("1", "RB", 0.0)]
    holes = find_lineup_holes(_lineup(players, ["FLEX"]), {"1": {"injury_status": "Out"}})

    items = recommend_fixes(holes, [_target(_player("99", "WR", 14.0))])

    assert items[0].fix.player.position == "WR"


def test_no_fix_offered_when_the_upgrade_is_inside_noise():
    players = [_player("1", "RB", 4.0)]
    holes = find_lineup_holes(_lineup(players, ["RB"]), {})

    items = recommend_fixes(holes, [_target(_player("99", "RB", 4.5))], min_gain=1.0)

    assert items[0].fix is None
    assert items[0].gain == 0.0


def test_one_target_is_not_recommended_for_two_different_holes():
    players = [_player("1", "RB", 0.0), _player("2", "RB", 0.0)]
    raw = {"1": {"injury_status": "Out"}, "2": {"injury_status": "Out"}}
    holes = find_lineup_holes(_lineup(players, ["RB", "RB"]), raw)
    targets = [_target(_player("99", "RB", 15.0)), _target(_player("98", "RB", 12.0))]

    items = recommend_fixes(holes, targets)

    assert {item.fix.player.player_id for item in items} == {"99", "98"}


def test_critical_holes_claim_the_best_target_before_weak_ones():
    players = [_player("1", "RB", 0.0), _player("2", "RB", 4.0)]
    holes = find_lineup_holes(_lineup(players, ["RB", "RB"]), {"1": {"injury_status": "Out"}})
    targets = [_target(_player("99", "RB", 18.0)), _target(_player("98", "RB", 9.0))]

    items = recommend_fixes(holes, targets)
    by_severity = {item.hole.severity: item.fix.player.player_id for item in items}

    assert by_severity["critical"] == "99"
    assert by_severity["weak"] == "98"


def test_bid_is_attached_when_the_league_uses_faab():
    players = [_player("1", "QB", 0.0)]
    holes = find_lineup_holes(_lineup(players, ["QB"]), {"1": {"injury_status": "Out"}})

    items = recommend_fixes(holes, [_target(_player("99", "QB", 17.6))], bids={"99": (20, 35)})

    assert items[0].bid == (20, 35)
