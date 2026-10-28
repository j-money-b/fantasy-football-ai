from ffai.models import PlayerProjection
from ffai.roster_moves import MOVE_WORTH_POINTS, bid_range, plan_moves

ROSTER_POSITIONS = ["QB", "WR", "K", "BN", "BN"]


def _player(player_id, position, points, unavailable=None):
    return PlayerProjection(
        player_id=player_id, name=f"{position}-{player_id}", position=position, team="KC",
        bye_week=None, points=points, data_source="test", unavailable=unavailable,
    )


def _future(**by_week):
    """week -> {player_id: points}, e.g. _future(w2={"1": 10.0})."""
    return {int(key[1:]): value for key, value in by_week.items()}


def _flat(points, *player_ids, weeks=(2, 3, 4)):
    return {week: {pid: points for pid in player_ids} for week in weeks}


def _merge(*futures):
    merged = {}
    for future in futures:
        for week, scores in future.items():
            merged.setdefault(week, {}).update(scores)
    return merged


def test_cuts_the_backup_qb_who_never_starts_for_a_wr_who_would():
    # Real Week 8 2026 shape: two QBs in a one-QB league, so the worse one
    # contributes nothing, while a free-agent WR outscores the starting WR.
    roster = [_player("qb1", "QB", 18.0), _player("qb2", "QB", 14.0), _player("wr1", "WR", 6.0),
              _player("k1", "K", 7.0), _player("wr2", "WR", 5.0)]
    future = _merge(_flat(18.0, "qb1"), _flat(14.0, "qb2"), _flat(6.0, "wr1"), _flat(7.0, "k1"),
                    _flat(5.0, "wr2"), _flat(11.0, "fa"))

    plan = plan_moves(roster, [_player("fa", "WR", 11.0)], ROSTER_POSITIONS, 1, future, {})

    [move] = plan.moves
    assert move.add.player_id == "fa"
    assert move.drop.player_id == "qb2"
    assert move.gain == 20.0  # (11 - 6) a week for four weeks
    assert any("wouldn't start a single week" in r for r in move.reasons)


def test_bench_upgrade_with_no_lineup_change_this_week_is_still_found():
    # The old waiver logic only asked "does he start THIS week?", so a free
    # agent on bye this week could never be recommended, however good.
    roster = [_player("qb1", "QB", 18.0), _player("wr1", "WR", 10.0), _player("k1", "K", 7.0),
              _player("wr2", "WR", 2.0), _player("wr3", "WR", 1.0)]
    future = _merge(_flat(18.0, "qb1"), _flat(10.0, "wr1"), _flat(7.0, "k1"), _flat(2.0, "wr2"),
                    _flat(1.0, "wr3"), _flat(15.0, "fa"))
    on_bye = _player("fa", "WR", 0.0, unavailable="on bye")

    plan = plan_moves(roster, [on_bye], ROSTER_POSITIONS, 1, future, {})

    [move] = plan.moves
    assert move.gain_this_week == 0.0
    assert move.gain == 15.0  # starts over wr1 in weeks 2-4: (15 - 10) x 3
    assert "pickup for the weeks after" in move.reasons[0]


def test_long_term_injured_player_counts_as_zero_for_the_whole_horizon():
    roster = [_player("qb1", "QB", 18.0), _player("wr1", "WR", 0.0, unavailable="IR"), _player("k1", "K", 7.0),
              _player("wr2", "WR", 4.0), _player("wr3", "WR", 3.0)]
    future = _merge(_flat(18.0, "qb1"), _flat(20.0, "wr1"), _flat(7.0, "k1"), _flat(4.0, "wr2"),
                    _flat(3.0, "wr3"), _flat(6.0, "fa"))
    raw = {"wr1": {"injury_status": "IR"}}

    plan = plan_moves(roster, [_player("fa", "WR", 6.0)], ROSTER_POSITIONS, 1, future, raw)

    # Without the IR rule his 20-pt future weeks would make him uncuttable and
    # the free agent worthless; with it, the free agent starts every week.
    assert plan.moves[0].gain == 8.0  # (6 - 4) x 4


def test_no_move_when_nothing_on_the_wire_clears_the_bar():
    roster = [_player("qb1", "QB", 18.0), _player("wr1", "WR", 10.0), _player("k1", "K", 7.0),
              _player("wr2", "WR", 2.0), _player("wr3", "WR", 1.0)]
    future = _merge(_flat(18.0, "qb1"), _flat(10.0, "wr1"), _flat(7.0, "k1"), _flat(2.0, "wr2"),
                    _flat(1.0, "wr3"), _flat(10.5, "fa"))

    plan = plan_moves(roster, [_player("fa", "WR", 10.5)], ROSTER_POSITIONS, 1, future, {})

    assert plan.moves == []  # +0.5 a week is noise
    assert 0 < plan.wire["WR"][0].gain < MOVE_WORTH_POINTS  # but the scan still shows him


def test_two_moves_never_cut_the_same_player():
    roster = [_player("qb1", "QB", 18.0), _player("wr1", "WR", 3.0), _player("k1", "K", 7.0),
              _player("wr2", "WR", 2.0), _player("wr3", "WR", 1.0)]
    future = _merge(_flat(18.0, "qb1"), _flat(3.0, "wr1"), _flat(7.0, "k1"), _flat(2.0, "wr2"),
                    _flat(1.0, "wr3"), _flat(12.0, "fa1"), _flat(11.0, "fa2"))
    free_agents = [_player("fa1", "WR", 12.0), _player("fa2", "WR", 11.0)]

    plan = plan_moves(roster, free_agents, ROSTER_POSITIONS + ["WR"], 1, future, {})

    drops = [m.drop.player_id for m in plan.moves]
    assert len(drops) == len(set(drops)) == 2


# --- bids ------------------------------------------------------------------

def test_kickers_and_defenses_never_get_real_money():
    bid, worth, market = bid_range(_player("k", "K", 9.0), gain=12.0, horizon_len=4, remaining_budget=95, week=8)

    assert bid == (1, 1)
    assert "never pay real money" in market


def test_bid_scales_with_points_and_budget_left():
    # $90 over 10 weeks = $9/week; at ~5 pts a week that's $1.80 a point.
    player = _player("wr", "WR", 12.0)

    _, small, _ = bid_range(player, gain=5.0, horizon_len=4, remaining_budget=90, week=8)
    _, big, _ = bid_range(player, gain=20.0, horizon_len=4, remaining_budget=90, week=8)

    assert small == 9
    assert big == 36


def test_hot_player_bid_goes_near_full_value_quiet_one_does_not():
    player = _player("wr", "WR", 12.0)

    hot, worth, _ = bid_range(player, gain=20.0, horizon_len=4, remaining_budget=90, week=8, trending=900_000)
    quiet, _, _ = bid_range(player, gain=20.0, horizon_len=4, remaining_budget=90, week=8, trending=None)

    assert hot[1] == worth
    assert quiet[1] < hot[0]


def test_bid_is_capped_at_half_the_remaining_budget():
    _, worth, _ = bid_range(_player("rb", "RB", 25.0), gain=200.0, horizon_len=4, remaining_budget=90, week=8)

    assert worth == 45


def test_a_defense_pickup_replaces_your_defense_not_a_position_player():
    roster_positions = ["QB", "WR", "DEF", "BN", "BN"]
    roster = [_player("qb1", "QB", 18.0), _player("wr1", "WR", 12.0), _player("d1", "DEF", 5.0),
              _player("wr2", "WR", 10.0), _player("wr3", "WR", 9.0)]
    future = _merge(_flat(18.0, "qb1"), _flat(12.0, "wr1"), _flat(5.0, "d1"), _flat(10.0, "wr2"),
                    _flat(9.0, "wr3"), _flat(9.0, "fa"))

    plan = plan_moves(roster, [_player("fa", "DEF", 9.0)], roster_positions, 1, future, {})

    [move] = plan.moves
    assert move.drop.player_id == "d1"
