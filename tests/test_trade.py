import pytest

from ffai.models import PlayerVorp, TradeParty
from ffai.trade import evaluate_trade, format_trade_evaluation, resolve_player_ids


def _pv(player_id, position, points, vorp, team="KC", bye_week=None):
    return PlayerVorp(
        player_id=player_id, name=f"{position}-{player_id}", position=position, team=team,
        bye_week=bye_week, points=points, data_source="test", vorp=vorp,
    )


# --- resolve_player_ids -----------------------------------------------------

def test_resolve_player_ids_exact_case_insensitive_match():
    players_raw = {"1": {"full_name": "Josh Allen"}}

    assert resolve_player_ids(["josh allen"], players_raw) == ["1"]


def test_resolve_player_ids_no_match_raises():
    with pytest.raises(ValueError, match="no player found"):
        resolve_player_ids(["Nobody"], {"1": {"full_name": "Josh Allen"}})


def test_resolve_player_ids_ambiguous_match_raises():
    players_raw = {"1": {"full_name": "Josh Allen"}, "2": {"full_name": "Josh Allen"}}

    with pytest.raises(ValueError, match="multiple players match"):
        resolve_player_ids(["Josh Allen"], players_raw)


# --- evaluate_trade ----------------------------------------------------------

def test_evaluate_trade_straight_swap_is_zero_sum_in_vorp():
    board = {
        "a1": _pv("a1", "RB", 10.0, vorp=5.0),
        "b1": _pv("b1", "WR", 12.0, vorp=8.0),
    }
    party_a = TradeParty(label="You", roster_player_ids=["a1"], sends_ids=["a1"])
    party_b = TradeParty(label="Rival", roster_player_ids=["b1"], sends_ids=["b1"])

    evaluation = evaluate_trade(party_a, party_b, board, roster_positions=["RB", "WR", "BN"])

    side_a, side_b = evaluation.sides
    assert side_a.value_sent == 5.0
    assert side_a.value_received == 8.0
    assert side_a.net_vorp == 3.0
    assert side_b.net_vorp == -3.0  # exact mirror -- what A gains, B gives up


def test_evaluate_trade_lineup_delta_flags_redundant_addition_despite_high_vorp():
    # Team A already starts an elite RB; receiving a second elite RB (only
    # one RB slot) carries real VORP but should add ~0 to the lineup total.
    board = {
        "star_rb": _pv("star_rb", "RB", 25.0, vorp=20.0),
        "another_star_rb": _pv("another_star_rb", "RB", 24.0, vorp=19.0),
        "throwaway_wr": _pv("throwaway_wr", "WR", 1.0, vorp=-5.0),
    }
    roster_positions = ["RB", "BN"]
    party_a = TradeParty(label="You", roster_player_ids=["star_rb", "throwaway_wr"], sends_ids=["throwaway_wr"])
    party_b = TradeParty(label="Rival", roster_player_ids=["another_star_rb"], sends_ids=["another_star_rb"])

    evaluation = evaluate_trade(party_a, party_b, board, roster_positions)

    side_a = evaluation.sides[0]
    assert side_a.value_received > 0  # big VORP gain on paper
    assert side_a.lineup_delta == 0.0  # but it doesn't crack the single RB slot behind star_rb


def test_evaluate_trade_lineup_delta_positive_when_filling_real_need():
    board = {
        "bench_wr": _pv("bench_wr", "WR", 5.0, vorp=1.0),
        "good_rb": _pv("good_rb", "RB", 18.0, vorp=12.0),
    }
    roster_positions = ["RB", "WR", "BN"]
    party_a = TradeParty(label="You", roster_player_ids=["bench_wr"], sends_ids=["bench_wr"])
    party_b = TradeParty(label="Rival", roster_player_ids=["good_rb"], sends_ids=["good_rb"])

    evaluation = evaluate_trade(party_a, party_b, board, roster_positions)

    side_a = evaluation.sides[0]
    assert side_a.lineup_delta == pytest.approx(13.0)  # +18 RB filled, -5 WR lost


def test_evaluate_trade_bye_week_collision_warning():
    board = {
        "rb_a": _pv("rb_a", "RB", 15.0, vorp=5.0, bye_week=7),
        "rb_b_incoming": _pv("rb_b_incoming", "RB", 14.0, vorp=4.0, bye_week=7),
        "wr_out": _pv("wr_out", "WR", 3.0, vorp=-2.0),
    }
    roster_positions = ["RB", "RB", "WR", "BN"]  # 2 RB slots -- collision matters here
    party_a = TradeParty(label="You", roster_player_ids=["rb_a", "wr_out"], sends_ids=["wr_out"])
    party_b = TradeParty(label="Rival", roster_player_ids=["rb_b_incoming"], sends_ids=["rb_b_incoming"])

    evaluation = evaluate_trade(party_a, party_b, board, roster_positions)

    side_a = evaluation.sides[0]
    assert len(side_a.bye_week_warnings) == 1
    assert "bye week 7" in side_a.bye_week_warnings[0]


def test_evaluate_trade_no_bye_collision_for_single_slot_position():
    board = {
        "qb_a": _pv("qb_a", "QB", 20.0, vorp=5.0, bye_week=9),
        "qb_b_incoming": _pv("qb_b_incoming", "QB", 19.0, vorp=4.0, bye_week=9),
    }
    # Only one QB slot -- you'd only ever start one anyway, sharing a bye is moot.
    roster_positions = ["QB", "BN"]
    party_a = TradeParty(label="You", roster_player_ids=["qb_a"], sends_ids=[])
    party_b = TradeParty(label="Rival", roster_player_ids=["qb_b_incoming"], sends_ids=["qb_b_incoming"])

    evaluation = evaluate_trade(party_a, party_b, board, roster_positions)

    assert evaluation.sides[0].bye_week_warnings == []


def test_evaluate_trade_verdict_win_win():
    # A has two RBs (one redundant, no WR at all) and B has two WRs (one
    # redundant, no RB at all) -- swapping the redundant piece for the
    # missing position genuinely helps both starting lineups.
    board = {
        "rb_a1": _pv("rb_a1", "RB", 15.0, vorp=10.0),
        "rb_a2": _pv("rb_a2", "RB", 2.0, vorp=-3.0),
        "wr_b1": _pv("wr_b1", "WR", 15.0, vorp=10.0),
        "wr_b2": _pv("wr_b2", "WR", 2.0, vorp=-3.0),
    }
    roster_positions = ["RB", "WR", "BN"]
    party_a = TradeParty(label="You", roster_player_ids=["rb_a1", "rb_a2"], sends_ids=["rb_a2"])
    party_b = TradeParty(label="Rival", roster_player_ids=["wr_b1", "wr_b2"], sends_ids=["wr_b2"])

    evaluation = evaluate_trade(party_a, party_b, board, roster_positions)

    assert evaluation.sides[0].lineup_delta > 0
    assert evaluation.sides[1].lineup_delta > 0
    assert "win-win" in evaluation.verdict


def test_evaluate_trade_verdict_favors_one_side():
    board = {
        "great_rb": _pv("great_rb", "RB", 20.0, vorp=15.0),
        "meh_wr": _pv("meh_wr", "WR", 5.0, vorp=0.0),
    }
    roster_positions = ["RB", "WR", "BN"]
    party_a = TradeParty(label="You", roster_player_ids=["meh_wr"], sends_ids=["meh_wr"])
    party_b = TradeParty(label="Rival", roster_player_ids=["great_rb"], sends_ids=["great_rb"])

    evaluation = evaluate_trade(party_a, party_b, board, roster_positions)

    assert "Favors You" in evaluation.verdict


# --- format_trade_evaluation --------------------------------------------------

def test_format_trade_evaluation_renders_both_sides_and_verdict():
    board = {
        "a1": _pv("a1", "RB", 10.0, vorp=5.0),
        "b1": _pv("b1", "WR", 12.0, vorp=8.0),
    }
    party_a = TradeParty(label="You", roster_player_ids=["a1"], sends_ids=["a1"])
    party_b = TradeParty(label="Rival", roster_player_ids=["b1"], sends_ids=["b1"])
    evaluation = evaluate_trade(party_a, party_b, board, roster_positions=["RB", "WR", "BN"])

    text = format_trade_evaluation(evaluation)

    assert "You:" in text
    assert "Rival:" in text
    assert "Sends: RB-a1" in text
    assert "Receives: WR-b1" in text
    assert "Verdict:" in text


# --- availability ----------------------------------------------------------
# Regression: the season board values every player at a healthy full-season
# projection. A player on the inactive list was priced at 139 season points
# and a QB with a dislocated elbow at 309, because nothing read the status
# field the brief had been using all along.

def _raw(**statuses):
    return {pid: {"injury_status": status} for pid, status in statuses.items()}


def _by_id(players):
    return {p.player_id: p for p in players}


def test_unavailable_player_received_is_flagged():
    board = _by_id([_pv("a", "RB", 139.4, vorp=-35.4), _pv("b", "RB", 200.0, vorp=20.0)])
    party_a = TradeParty(label="Me", roster_player_ids=["b"], sends_ids=["b"])
    party_b = TradeParty(label="Them", roster_player_ids=["a"], sends_ids=["a"])

    result = evaluate_trade(party_a, party_b, board, ["RB"], players_raw=_raw(a="NA"))

    warnings = result.sides[0].availability_warnings
    assert any("CANNOT PLAY" in w for w in warnings)
    assert any("8.2 pts of it per game missed" in w for w in warnings)


def test_verdict_refuses_to_judge_when_a_player_cannot_play():
    board = _by_id([_pv("a", "RB", 139.4, vorp=-35.4), _pv("b", "RB", 200.0, vorp=20.0)])
    party_a = TradeParty(label="Me", roster_player_ids=["b"], sends_ids=["b"])
    party_b = TradeParty(label="Them", roster_player_ids=["a"], sends_ids=["a"])

    result = evaluate_trade(party_a, party_b, board, ["RB"], players_raw=_raw(a="Out"))

    assert result.projections_trustworthy is False
    assert "CANNOT BE JUDGED ON PROJECTIONS ALONE" in result.verdict


def test_questionable_is_noted_but_does_not_void_the_verdict():
    board = _by_id([_pv("a", "RB", 226.5, vorp=62.9), _pv("b", "RB", 150.0, vorp=-20.0)])
    party_a = TradeParty(label="Me", roster_player_ids=["b"], sends_ids=["b"])
    party_b = TradeParty(label="Them", roster_player_ids=["a"], sends_ids=["a"])

    result = evaluate_trade(party_a, party_b, board, ["RB"], players_raw=_raw(a="Questionable"))

    assert result.projections_trustworthy is True
    assert "CANNOT BE JUDGED" not in result.verdict
    assert any("usually still plays" in w for w in result.sides[0].availability_warnings)


def test_healthy_trade_is_unchanged_and_still_gets_a_real_verdict():
    board = _by_id([_pv("a", "RB", 200.0, vorp=20.0), _pv("b", "RB", 150.0, vorp=-20.0)])
    party_a = TradeParty(label="Me", roster_player_ids=["b"], sends_ids=["b"])
    party_b = TradeParty(label="Them", roster_player_ids=["a"], sends_ids=["a"])

    result = evaluate_trade(party_a, party_b, board, ["RB"], players_raw={})

    assert result.projections_trustworthy is True
    assert result.sides[0].availability_warnings == []


def test_injury_detail_is_surfaced_not_just_the_status():
    board = _by_id([_pv("a", "QB", 308.7, vorp=12.2), _pv("b", "RB", 150.0, vorp=-20.0)])
    party_a = TradeParty(label="Me", roster_player_ids=["b"], sends_ids=["b"])
    party_b = TradeParty(label="Them", roster_player_ids=["a"], sends_ids=["a"])
    raw = {"a": {"injury_status": "Out", "injury_body_part": "Elbow", "injury_notes": "Dislocated"}}

    result = evaluate_trade(party_a, party_b, board, ["QB"], players_raw=raw)

    assert any("Elbow - Dislocated" in w for w in result.sides[0].availability_warnings)


def test_unavailable_player_still_starting_after_the_trade_is_flagged():
    """The two players swapped can both be perfectly healthy and the verdict
    still be computed against a lineup total that assumes a ruled-out starter
    suits up. Flagging only the traded players misses that entirely."""
    board = _by_id([
        _pv("mine-qb", "QB", 308.7, vorp=12.2),
        _pv("mine-rb", "RB", 150.0, vorp=-20.0),
        _pv("theirs", "RB", 200.0, vorp=20.0),
    ])
    party_a = TradeParty(label="Me", roster_player_ids=["mine-qb", "mine-rb"], sends_ids=["mine-rb"])
    party_b = TradeParty(label="Them", roster_player_ids=["theirs"], sends_ids=["theirs"])
    raw = {"mine-qb": {"injury_status": "Out", "injury_body_part": "Elbow", "injury_notes": "Dislocated"}}

    result = evaluate_trade(party_a, party_b, board, ["QB", "RB"], players_raw=raw)

    warnings = result.sides[0].availability_warnings
    assert any("still counts QB-mine-qb at QB" in w for w in warnings)
    assert any("inflated" in w for w in warnings)
    # The traded players are both healthy, so the verdict itself stands.
    assert result.projections_trustworthy is True
