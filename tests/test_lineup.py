from ffai.models import PlayerProjection
from ffai.lineup import format_lineup, optimize_lineup


def _player(player_id, position, points):
    return PlayerProjection(
        player_id=player_id,
        name=f"{position}-{player_id}",
        position=position,
        team="XXX",
        bye_week=None,
        points=points,
        data_source="test",
    )


def test_optimize_lineup_fills_dedicated_slots_by_points():
    roster_positions = ["QB", "RB", "RB", "WR", "WR", "BN", "BN"]
    players = [
        _player("qb1", "QB", 20),
        _player("rb1", "RB", 15),
        _player("rb2", "RB", 12),
        _player("rb3", "RB", 8),
        _player("wr1", "WR", 18),
        _player("wr2", "WR", 10),
    ]

    result = optimize_lineup(players, roster_positions)

    started = {s.slot: s.player.player_id for s in result.slots}
    assert started["QB"] == "qb1"
    assert {p for slot, p in started.items() if slot == "RB"} <= {"rb1", "rb2"}
    bench_ids = {p.player_id for p in result.bench}
    assert "rb3" in bench_ids
    assert result.total_points == 20 + 15 + 12 + 18 + 10


def test_optimize_lineup_flex_slot_takes_best_remaining_eligible_player():
    # rb1/rb2 are interchangeable at RB, so either may land in the RB slot
    # vs. FLEX -- what must hold is the *set* of starters (all 3 of
    # rb1/rb2/wr1, worth 20+18+15=53) and that te1 (10, edged out by rb2 for
    # the FLEX spot) is benched.
    roster_positions = ["RB", "WR", "FLEX", "BN"]
    players = [
        _player("rb1", "RB", 20),
        _player("rb2", "RB", 18),  # should win the FLEX slot over te1
        _player("wr1", "WR", 15),
        _player("te1", "TE", 10),  # FLEX-eligible but lower points than rb2
    ]

    result = optimize_lineup(players, roster_positions)

    started_ids = {s.player.player_id for s in result.slots if s.player is not None}
    assert started_ids == {"rb1", "rb2", "wr1"}
    assert result.total_points == 20 + 18 + 15
    assert {p.player_id for p in result.bench} == {"te1"}


def test_optimize_lineup_is_exact_when_flex_types_overlap_without_nesting():
    # WRRB_FLEX (WR/RB) and REC_FLEX (WR/TE) overlap in WR but neither
    # nests the other -- vorp.compute_replacement_levels' "narrowest first"
    # heuristic isn't guaranteed optimal here, but optimize_lineup must be.
    # Only 2 starting slots for 3 players, so a real trade-off exists.
    # Optimal: wr1 -> WRRB_FLEX (20), te1 -> REC_FLEX (19), wr2 (10) benched
    # (39 total) -- NOT wr1/wr2 both starting at the expense of te1 (worse).
    roster_positions = ["WRRB_FLEX", "REC_FLEX", "BN"]
    players = [
        _player("wr1", "WR", 20),
        _player("te1", "TE", 19),
        _player("wr2", "WR", 10),
    ]

    result = optimize_lineup(players, roster_positions)

    assert result.total_points == 39
    assert {p.player_id for p in result.bench} == {"wr2"}


def test_optimize_lineup_leaves_slot_empty_when_no_eligible_player_available():
    roster_positions = ["QB", "DEF", "BN"]
    players = [_player("qb1", "QB", 20)]

    result = optimize_lineup(players, roster_positions)

    def_slot = next(s for s in result.slots if s.slot == "DEF")
    assert def_slot.player is None
    assert result.total_points == 20


def test_format_lineup_lists_starters_and_bench():
    roster_positions = ["QB", "BN"]
    players = [_player("qb1", "QB", 20.0), _player("qb2", "QB", 10.0)]

    result = optimize_lineup(players, roster_positions)
    text = format_lineup(result)

    assert "Optimal lineup (20.0 pts):" in text
    assert "QB-qb1" in text
    assert "Bench:" in text
    assert "QB-qb2" in text
