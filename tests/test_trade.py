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
