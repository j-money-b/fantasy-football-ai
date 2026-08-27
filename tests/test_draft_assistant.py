from ffai.cache import Cache
from ffai.draft_assistant import DraftAssistant, format_recommendation
from ffai.lineup import optimize_lineup
from ffai.models import PlayerVorp
from ffai.projections import ConsensusEntry
from ffai.sleeper_client import SleeperAPIError


class FakeDraftClient:
    def __init__(self, picks_sequence):
        # each poll_once() call consumes the next entry in this list
        self.picks_sequence = picks_sequence
        self.calls = 0

    def get_draft_picks(self, draft_id):
        picks = self.picks_sequence[min(self.calls, len(self.picks_sequence) - 1)]
        self.calls += 1
        return picks


class FailingDraftClient:
    def get_draft_picks(self, draft_id):
        raise SleeperAPIError("simulated failure")


def _player(player_id, position, name=None, team="XXX", vorp=0.0, bye_week=None, tier=None, data_source="sleeper"):
    return PlayerVorp(
        player_id=player_id, name=name or player_id, position=position, team=team,
        bye_week=bye_week, points=vorp, data_source=data_source, vorp=vorp, tier=tier,
    )


def _pick(pick_no, player_id, draft_slot=1):
    return {"pick_no": pick_no, "player_id": player_id, "draft_slot": draft_slot}


ROSTER_POSITIONS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "K", "DEF", "BN", "BN"]


def test_poll_once_returns_only_new_picks_across_polls(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    board = [_player("a", "RB", vorp=20), _player("b", "WR", vorp=15)]
    picks_sequence = [
        [_pick(1, "a")],
        [_pick(1, "a"), _pick(2, "b")],
    ]
    assistant = DraftAssistant(FakeDraftClient(picks_sequence), cache, "draft1", board, ROSTER_POSITIONS, my_draft_slot=1)

    first = assistant.poll_once()
    second = assistant.poll_once()

    assert [p["player_id"] for p in first] == ["a"]
    assert [p["player_id"] for p in second] == ["b"]
    assert assistant.drafted_player_ids == {"a", "b"}


def test_poll_once_tracks_my_drafted_players(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    board = [_player("a", "RB", vorp=20), _player("b", "WR", vorp=15)]
    picks_sequence = [[_pick(1, "a", draft_slot=1), _pick(2, "b", draft_slot=2)]]
    assistant = DraftAssistant(FakeDraftClient(picks_sequence), cache, "draft1", board, ROSTER_POSITIONS, my_draft_slot=1)

    assistant.poll_once()

    assert [p.player_id for p in assistant.my_drafted_players] == ["a"]


def test_poll_once_swallows_api_error_and_returns_empty(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    assistant = DraftAssistant(FailingDraftClient(), cache, "draft1", [], ROSTER_POSITIONS, my_draft_slot=1)

    assert assistant.poll_once() == []


def test_available_players_excludes_drafted(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    board = [_player("a", "RB", vorp=20), _player("b", "WR", vorp=15)]
    assistant = DraftAssistant(FakeDraftClient([[_pick(1, "a")]]), cache, "draft1", board, ROSTER_POSITIONS, my_draft_slot=1)

    assistant.poll_once()

    assert [p.player_id for p in assistant.available_players()] == ["b"]


def test_detect_positional_run(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    board = [_player(str(i), "RB", vorp=10) for i in range(3)] + [_player("wr1", "WR", vorp=8)]
    picks = [_pick(1, "0"), _pick(2, "1"), _pick(3, "2"), _pick(4, "wr1")]
    assistant = DraftAssistant(FakeDraftClient([picks]), cache, "draft1", board, ROSTER_POSITIONS, my_draft_slot=1)

    assistant.poll_once()

    assert assistant.detect_positional_run() == "RB"


def test_detect_positional_run_none_when_no_run(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    board = [_player("a", "RB", vorp=10), _player("b", "WR", vorp=8)]
    picks = [_pick(1, "a"), _pick(2, "b")]
    assistant = DraftAssistant(FakeDraftClient([picks]), cache, "draft1", board, ROSTER_POSITIONS, my_draft_slot=1)

    assistant.poll_once()

    assert assistant.detect_positional_run() is None


def test_recommend_picks_best_marginal_value_with_tier_reasoning(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    board = [_player("a", "RB", vorp=20, tier=1), _player("b", "WR", vorp=12, tier=2)]
    assistant = DraftAssistant(FakeDraftClient([[]]), cache, "draft1", board, ROSTER_POSITIONS, my_draft_slot=1)

    rec = assistant.recommend()

    assert rec.player.player_id == "a"
    assert rec.degraded is False
    assert any("adds 20.0 pts" in r for r in rec.reasons)
    assert any("Tier 1" in r for r in rec.reasons)
    assert any("no RB rostered yet" in r for r in rec.reasons)


def test_recommend_healthy_stops_chasing_a_filled_position(tmp_path):
    """The core repro of the real bug: a 3rd TE that still looks great by
    raw VORP (a steep post-tier1 cliff inflates it) but scores fewer actual
    points than every starter already rostered must lose to a candidate at
    a position with real open roster capacity (K), even though the K's raw
    VORP is far lower."""
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    my_drafted_players = [
        PlayerVorp(player_id="rb1", name="RB1", position="RB", team="AAA", bye_week=None, points=40, data_source="sleeper", vorp=15, tier=1),
        PlayerVorp(player_id="rb2", name="RB2", position="RB", team="BBB", bye_week=None, points=35, data_source="sleeper", vorp=12, tier=1),
        PlayerVorp(player_id="wr1", name="WR1", position="WR", team="CCC", bye_week=None, points=38, data_source="sleeper", vorp=14, tier=1),
        PlayerVorp(player_id="wr2", name="WR2", position="WR", team="DDD", bye_week=None, points=33, data_source="sleeper", vorp=11, tier=1),
        PlayerVorp(player_id="te1", name="TE1", position="TE", team="EEE", bye_week=None, points=30, data_source="sleeper", vorp=20, tier=1),
        PlayerVorp(player_id="flex_rb", name="FlexRB", position="RB", team="FFF", bye_week=None, points=28, data_source="sleeper", vorp=8, tier=2),
        PlayerVorp(player_id="flex_wr", name="FlexWR", position="WR", team="GGG", bye_week=None, points=26, data_source="sleeper", vorp=6, tier=2),
    ]
    te_candidate = PlayerVorp(
        player_id="te2", name="TE2", position="TE", team="HHH", bye_week=None,
        points=20, data_source="sleeper", vorp=37, tier=1,
    )
    k_candidate = PlayerVorp(
        player_id="k1", name="K1", position="K", team="III", bye_week=None,
        points=9, data_source="sleeper", vorp=3, tier=3,
    )

    board = [te_candidate, k_candidate]
    assistant = DraftAssistant(FakeDraftClient([[]]), cache, "draft1", board, ROSTER_POSITIONS, my_draft_slot=1)
    assistant.my_drafted_players = my_drafted_players

    rec = assistant.recommend()

    assert rec.player.player_id == "k1"
    assert any("adds 9.0 pts" in r for r in rec.reasons)


def test_recommend_healthy_prefers_real_cover_once_lineup_is_full(tmp_path):
    """Once every starting slot is saturated, marginal value is 0 for
    everyone and the pick has to be broken some other way.

    This used to fall through to VORP, and that was wrong in the way VORP
    is always wrong as a tiebreak here: it is a season-long valuation
    measured against a replacement level computed off each position's own
    pool, so the thinner the pool the more it flatters its players. A real
    mock spent its FINAL pick on a fourth tight end at VORP -1.1, behind a
    starter who never leaves the lineup.

    The tie now breaks on how much cover the player actually provides.
    CandA is the lower-VORP candidate (5 vs 9) but plays the position with
    more starting slots to lose and the higher miss rate, so he is worth
    more as depth and wins despite the worse static number."""
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    starters = [
        PlayerVorp(player_id="qb", name="QB", position="QB", team="Z1", bye_week=None, points=100, data_source="sleeper", vorp=50, tier=1),
        PlayerVorp(player_id="rb1", name="RB1", position="RB", team="Z2", bye_week=None, points=100, data_source="sleeper", vorp=50, tier=1),
        PlayerVorp(player_id="rb2", name="RB2", position="RB", team="Z3", bye_week=None, points=100, data_source="sleeper", vorp=50, tier=1),
        PlayerVorp(player_id="wr1", name="WR1", position="WR", team="Z4", bye_week=None, points=100, data_source="sleeper", vorp=50, tier=1),
        PlayerVorp(player_id="wr2", name="WR2", position="WR", team="Z5", bye_week=None, points=100, data_source="sleeper", vorp=50, tier=1),
        PlayerVorp(player_id="te", name="TE", position="TE", team="Z6", bye_week=None, points=100, data_source="sleeper", vorp=50, tier=1),
        PlayerVorp(player_id="flexrb", name="FlexRB", position="RB", team="Z7", bye_week=None, points=100, data_source="sleeper", vorp=50, tier=1),
        PlayerVorp(player_id="flexwr", name="FlexWR", position="WR", team="Z8", bye_week=None, points=100, data_source="sleeper", vorp=50, tier=1),
        PlayerVorp(player_id="k", name="K", position="K", team="Z9", bye_week=None, points=100, data_source="sleeper", vorp=50, tier=1),
        PlayerVorp(player_id="def", name="DEF", position="DEF", team="Z10", bye_week=None, points=100, data_source="sleeper", vorp=50, tier=1),
    ]
    cand_a = PlayerVorp(player_id="a", name="CandA", position="RB", team="Y1", bye_week=None, points=10, data_source="sleeper", vorp=5, tier=2)
    cand_b = PlayerVorp(player_id="b", name="CandB", position="WR", team="Y2", bye_week=None, points=8, data_source="sleeper", vorp=9, tier=2)

    board = [cand_a, cand_b]
    assistant = DraftAssistant(FakeDraftClient([[]]), cache, "draft1", board, ROSTER_POSITIONS, my_draft_slot=1)
    assistant.my_drafted_players = starters

    rec = assistant.recommend()

    assert rec.player.player_id == "a"
    assert any("Wouldn't crack your starting lineup" in r for r in rec.reasons)


def test_recommend_healthy_stops_stacking_redundant_cover_at_a_thin_position(tmp_path):
    """Regression for the four-tight-end mock draft (real draft
    1398505784274350080, slot 3).

    The roster already starts one TE and carries a backup, so a third TE
    cannot cover anything: there is only one TE slot to lose. But the old
    single-loss depth check did not return 0 for him -- it returned the
    UPGRADE over the existing backup (+2 here, +2.10 in the real draft),
    which cleared a bare `> 0` gate and kept TE eligible for bench picks
    round after round. RB depth meanwhile scored exactly 0.00, because one
    backup covers one loss, and was dropped from consideration entirely.

    Asking about two simultaneous losses separates them: TE has only one
    starter to lose so it stays ~0, while losing two of RB1/RB2/FLEX
    leaves a hole no one on the roster can fill."""
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    starters = [
        PlayerVorp(player_id="qb", name="QB", position="QB", team="Z1", bye_week=None, points=100, data_source="sleeper", vorp=50, tier=1),
        PlayerVorp(player_id="rb1", name="RB1", position="RB", team="Z2", bye_week=None, points=100, data_source="sleeper", vorp=50, tier=1),
        PlayerVorp(player_id="rb2", name="RB2", position="RB", team="Z3", bye_week=None, points=90, data_source="sleeper", vorp=45, tier=1),
        PlayerVorp(player_id="wr1", name="WR1", position="WR", team="Z4", bye_week=None, points=100, data_source="sleeper", vorp=50, tier=1),
        PlayerVorp(player_id="wr2", name="WR2", position="WR", team="Z5", bye_week=None, points=90, data_source="sleeper", vorp=45, tier=1),
        PlayerVorp(player_id="te1", name="TE1", position="TE", team="Z6", bye_week=None, points=80, data_source="sleeper", vorp=40, tier=1),
        PlayerVorp(player_id="flexrb", name="FlexRB", position="RB", team="Z7", bye_week=None, points=70, data_source="sleeper", vorp=35, tier=1),
        PlayerVorp(player_id="flexwr", name="FlexWR", position="WR", team="Z8", bye_week=None, points=70, data_source="sleeper", vorp=35, tier=1),
        PlayerVorp(player_id="k", name="K", position="K", team="Z9", bye_week=None, points=50, data_source="sleeper", vorp=20, tier=1),
        PlayerVorp(player_id="def", name="DEF", position="DEF", team="Z10", bye_week=None, points=50, data_source="sleeper", vorp=20, tier=1),
        # The backup that already covers the one TE slot.
        PlayerVorp(player_id="te2", name="TE2", position="TE", team="Z11", bye_week=None, points=60, data_source="sleeper", vorp=30, tier=2),
    ]
    # A third TE, marginally better than the backup already rostered, and
    # carrying the inflated VORP a thin pool produces.
    te_redundant = PlayerVorp(player_id="te3", name="TE3", position="TE", team="Y1", bye_week=None, points=62, data_source="sleeper", vorp=31, tier=2)
    rb_depth = PlayerVorp(player_id="rb4", name="RB4", position="RB", team="Y2", bye_week=None, points=55, data_source="sleeper", vorp=12, tier=3)

    board = [te_redundant, rb_depth]
    assistant = DraftAssistant(FakeDraftClient([[]]), cache, "draft1", board, ROSTER_POSITIONS, my_draft_slot=1)
    assistant.my_drafted_players = starters

    rec = assistant.recommend()

    assert rec.player.player_id == "rb4"


def _vorp_player(player_id, position, points, vorp, tier=1):
    return PlayerVorp(
        player_id=player_id, name=player_id, position=position, team="XXX",
        bye_week=None, points=points, data_source="sleeper", vorp=vorp, tier=tier,
    )


def test_recommend_prioritizes_position_being_drafted_out_from_under_you(tmp_path):
    """Regression for two real mock drafts that left replacement-level RBs
    starting. RB and WR both have open slots here and WR grades higher on
    static value (VORP 15 vs 10), so ranking by VORP takes the WR -- but
    the draft itself shows RBs coming off the board far faster than WRs,
    and the RB pool craters right after the top few while WR stays deep.
    The tool has to notice that from the live pick feed and take the RB."""
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    rbs = [_vorp_player(f"rb{i}", "RB", points=200 - i, vorp=10 - i) for i in range(10)]
    rbs += [_vorp_player(f"rb_cliff{i}", "RB", points=160 - i, vorp=-30 - i, tier=2) for i in range(10)]
    wrs = [_vorp_player(f"wr{i}", "WR", points=205 - i, vorp=15 - i) for i in range(20)]
    board = rbs + wrs

    # 21 picks in, 15 of them RB -- an unmistakable run on the position.
    taken = [f"rb_gone{i}" for i in range(15)] + [f"wr_gone{i}" for i in range(6)]
    board += [_vorp_player(pid, "RB" if pid.startswith("rb") else "WR", points=1, vorp=-99, tier=2) for pid in taken]
    picks = [_pick(i + 1, pid, draft_slot=(i % 10) + 1) for i, pid in enumerate(taken)]

    assistant = DraftAssistant(
        FakeDraftClient([picks]), cache, "draft1", board, ROSTER_POSITIONS, my_draft_slot=1, total_rosters=10
    )
    assistant.poll_once()

    rec = assistant.recommend()

    assert rec.player.position == "RB"
    assert rec.player.player_id == "rb0"
    assert any("Cost of waiting" in r for r in rec.reasons)


def test_recommend_never_boosts_a_below_replacement_player(tmp_path):
    """Regression for a real mock draft: a kicker sitting below replacement
    (negative VORP, but still enough raw points to pass the marginal-value
    gate since the K slot was open) got boosted above a positive-VORP DEF,
    because a deep bench tail of near-worthless kickers made the lookahead
    land on an even more negative fallback -- producing a large "cliff"
    bonus for a player nobody would ever actually reach for. Below
    replacement must stay below replacement regardless of how much worse
    the tail behind it gets."""
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    board = [
        PlayerVorp(player_id="k1", name="K1", position="K", team="AAA", bye_week=None, points=59, data_source="sleeper", vorp=-13, tier=1),
        PlayerVorp(player_id="k2", name="K2", position="K", team="BBB", bye_week=None, points=50, data_source="sleeper", vorp=-22, tier=1),
        PlayerVorp(player_id="k3", name="K3", position="K", team="CCC", bye_week=None, points=45, data_source="sleeper", vorp=-27, tier=1),
        PlayerVorp(player_id="k4", name="K4", position="K", team="DDD", bye_week=None, points=1, data_source="sleeper", vorp=-71, tier=2),
        PlayerVorp(player_id="def1", name="DEF1", position="DEF", team="EEE", bye_week=None, points=90, data_source="sleeper", vorp=18, tier=1),
    ]
    assistant = DraftAssistant(
        FakeDraftClient([[]]), cache, "draft1", board, ROSTER_POSITIONS, my_draft_slot=4, total_rosters=4
    )

    rec = assistant.recommend()

    assert rec.player.player_id == "def1"


def test_recommend_does_not_let_smooth_decline_look_like_a_cliff(tmp_path):
    """Regression: a position that's just declining steadily (a deep QB
    class) must not get boosted enough to leapfrog a clearly-better
    candidate at another still-deep position (WR) -- only a REAL cliff (a
    big drop, not merely being a few ranks further down) should be able to
    flip the ranking."""
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    board = (
        [_player(f"qb{i}", "QB", vorp=v, tier=i + 1) for i, v in enumerate([24, 20, 16, 12, 8, 4])]
        + [_player(f"wr{i}", "WR", vorp=v, tier=1) for i, v in enumerate([33, 30, 27, 24, 21, 18])]
    )
    assistant = DraftAssistant(
        FakeDraftClient([[]]), cache, "draft1", board, ROSTER_POSITIONS, my_draft_slot=4, total_rosters=4
    )

    rec = assistant.recommend()

    assert rec.player.position == "WR"


def test_recommend_prefers_consensus_backed_alternative_in_same_tier(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    board = [
        _player("outlier", "TE", vorp=40, tier=1, name="Outlier TE"),
        _player("safe", "TE", vorp=35, tier=1, name="Safe TE"),
    ]
    consensus = {"TE": [ConsensusEntry(name="Safe TE", team="XXX", position="TE", raw_stats={}, player_id="safe")]}
    assistant = DraftAssistant(
        FakeDraftClient([[]]), cache, "draft1", board, ROSTER_POSITIONS, my_draft_slot=1, consensus_top10=consensus
    )

    rec = assistant.recommend()

    assert rec.player.player_id == "safe"
    assert any("Preferred over Outlier TE" in r for r in rec.reasons)


def test_recommend_does_not_swap_across_positions_for_consensus_dampening(tmp_path):
    """Regression: global VORP tiers span positions, so matching on tier
    alone once let this suggest a DEF as a 'close in value' alternative to
    a non-consensus-backed WR. The alt search must be scoped to the same
    position, and a position with no FantasyPros coverage at all (DEF)
    must never count as 'safer' just because it has no data to contradict."""
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    board = [
        _player("outlier_wr", "WR", vorp=40, tier=1, name="Outlier WR"),
        _player("def1", "DEF", vorp=38, tier=1, name="Some DEF"),
    ]
    consensus = {"WR": [ConsensusEntry(name="Someone Else", team="XXX", position="WR", raw_stats={}, player_id="elsewhere")]}
    assistant = DraftAssistant(
        FakeDraftClient([[]]), cache, "draft1", board, ROSTER_POSITIONS, my_draft_slot=1, consensus_top10=consensus
    )

    rec = assistant.recommend()

    assert rec.player.player_id == "outlier_wr"
    assert any("no comparable consensus-backed alternative" in r for r in rec.reasons)


def test_recommend_notes_caution_when_no_consensus_backed_alternative_exists(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    board = [_player("only", "TE", vorp=40, tier=1, name="Only TE")]
    consensus = {
        "TE": [ConsensusEntry(name="Someone Else", team="XXX", position="TE", raw_stats={}, player_id="elsewhere")]
    }
    assistant = DraftAssistant(
        FakeDraftClient([[]]), cache, "draft1", board, ROSTER_POSITIONS, my_draft_slot=1, consensus_top10=consensus
    )

    rec = assistant.recommend()

    assert rec.player.player_id == "only"
    assert any(
        "isn't in FantasyPros' consensus top 10" in r and "no comparable consensus-backed alternative" in r
        for r in rec.reasons
    )


def test_picks_until_my_turn_computes_snake_distance(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    assistant = DraftAssistant(
        FakeDraftClient([[]]), cache, "draft1", [], ROSTER_POSITIONS, my_draft_slot=4, total_rosters=4
    )

    assert assistant._picks_until_my_turn() == 3

    # picks 1-4 (round 1) are made; pick 5 (round 2, snake-reversed) is slot 4's turn again
    assistant.picks = [_pick(i, f"p{i}") for i in range(1, 5)]
    assert assistant._picks_until_my_turn() == 0


def test_picks_until_my_turn_returns_none_without_total_rosters(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    assistant = DraftAssistant(FakeDraftClient([[]]), cache, "draft1", [], ROSTER_POSITIONS, my_draft_slot=1)

    assert assistant._picks_until_my_turn() is None


def test_recommend_explains_the_cost_of_waiting(tmp_path):
    """The reasoning has to name what waiting actually costs -- which
    player you'd likely be left with, and how much worse they are."""
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    rbs = [_vorp_player(f"rb{i}", "RB", points=200 - i * 12, vorp=30 - i * 12) for i in range(8)]
    wrs = [_vorp_player(f"wr{i}", "WR", points=190 - i, vorp=20 - i) for i in range(8)]
    taken = [f"gone{i}" for i in range(15)]
    board = rbs + wrs + [_vorp_player(pid, "RB", points=1, vorp=-99, tier=2) for pid in taken]
    picks = [_pick(i + 1, pid, draft_slot=(i % 10) + 1) for i, pid in enumerate(taken)]

    assistant = DraftAssistant(
        FakeDraftClient([picks]), cache, "draft1", board, ROSTER_POSITIONS, my_draft_slot=1, total_rosters=10
    )
    assistant.poll_once()

    rec = assistant.recommend()

    assert any("Cost of waiting" in r for r in rec.reasons)


def test_recommend_never_stacks_a_second_defense_in_bench_rounds(tmp_path):
    """Regression for a real mock draft that ended with four defenses. With
    every starting slot full, marginal lineup value is 0 for everyone, so
    ranking falls through to raw VORP -- where DEF screens deceptively well
    because its replacement level comes off a very thin pool. A backup DEF
    is worthless when the waiver wire refills the position weekly, so once
    the DEF slot is covered another one must never be recommended over a
    real skill-position bench player."""
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    starters = [
        _vorp_player("qb", "QB", points=300, vorp=50),
        _vorp_player("rb1", "RB", points=200, vorp=50),
        _vorp_player("rb2", "RB", points=200, vorp=50),
        _vorp_player("wr1", "WR", points=200, vorp=50),
        _vorp_player("wr2", "WR", points=200, vorp=50),
        _vorp_player("te", "TE", points=200, vorp=50),
        _vorp_player("flex1", "RB", points=200, vorp=50),
        _vorp_player("flex2", "WR", points=200, vorp=50),
        _vorp_player("k", "K", points=100, vorp=5),
        _vorp_player("def", "DEF", points=110, vorp=20),
    ]
    board = [
        _vorp_player("def2", "DEF", points=105, vorp=16),
        _vorp_player("rb_depth", "RB", points=150, vorp=-20),
    ]
    assistant = DraftAssistant(
        FakeDraftClient([[]]), cache, "draft1", board, ROSTER_POSITIONS, my_draft_slot=4, total_rosters=4
    )
    assistant.my_drafted_players = starters

    rec = assistant.recommend()

    assert rec.player.player_id == "rb_depth"


def test_recommend_flags_bye_week_collision(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    board = [_player("a", "RB", vorp=20, bye_week=7, name="Backup RB")]
    assistant = DraftAssistant(FakeDraftClient([[]]), cache, "draft1", board, ROSTER_POSITIONS, my_draft_slot=1)
    assistant.my_drafted_players = [_player("existing", "RB", vorp=30, bye_week=7, name="Starter RB")]

    rec = assistant.recommend()

    assert any("Bye-week collision" in r and "Starter RB" in r for r in rec.reasons)


def test_recommend_flags_qb_wr_stacking(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    board = [_player("wr1", "WR", vorp=15, team="BUF", name="Some WR")]
    assistant = DraftAssistant(FakeDraftClient([[]]), cache, "draft1", board, ROSTER_POSITIONS, my_draft_slot=1)
    assistant.my_drafted_players = [_player("qb1", "QB", vorp=25, team="BUF", name="Josh Allen")]

    rec = assistant.recommend()

    assert any("Stacks with your QB Josh Allen" in r for r in rec.reasons)


def test_recommend_notes_consensus_agreement(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    board = [_player("a", "RB", vorp=20)]
    consensus = {"RB": [ConsensusEntry(name="a", team="XXX", position="RB", raw_stats={}, player_id="a")]}
    assistant = DraftAssistant(
        FakeDraftClient([[]]), cache, "draft1", board, ROSTER_POSITIONS, my_draft_slot=1, consensus_top10=consensus
    )

    rec = assistant.recommend()

    assert any("FantasyPros' consensus top 10" in r for r in rec.reasons)


def test_recommend_degraded_mode_ranks_by_roster_need_and_labels_banner(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    # both have vorp=0 (degraded); QB is unfilled, RB is not requested as urgently
    board = [_player("rb1", "RB", vorp=0.0, data_source="none"), _player("qb1", "QB", vorp=0.0, data_source="none")]
    assistant = DraftAssistant(
        FakeDraftClient([[]]), cache, "draft1", board, ["QB", "BN"], my_draft_slot=1, board_degraded=True
    )

    rec = assistant.recommend()

    assert rec.player.player_id == "qb1"
    assert rec.degraded is True
    assert any("NO PROJECTIONS AVAILABLE" in r for r in rec.reasons)


def test_recommend_returns_none_when_board_exhausted(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    board = [_player("a", "RB", vorp=20)]
    assistant = DraftAssistant(FakeDraftClient([[_pick(1, "a")]]), cache, "draft1", board, ROSTER_POSITIONS, my_draft_slot=1)
    assistant.poll_once()

    assert assistant.recommend() is None


def test_format_recommendation_none():
    assert format_recommendation(None) == "No available players remain on the board."


def test_format_recommendation_includes_player_and_reasons(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    board = [_player("a", "RB", vorp=20, name="Test Back", team="DET")]
    assistant = DraftAssistant(FakeDraftClient([[]]), cache, "draft1", board, ROSTER_POSITIONS, my_draft_slot=1)

    text = format_recommendation(assistant.recommend())

    assert "Test Back (RB, DET)" in text
    assert text.startswith("Recommended pick:")


def test_recommend_flags_coin_flip_when_same_position_alternatives_are_within_noise(tmp_path):
    # Mirrors the real mock that motivated this: Chase Brown recommended at
    # 1.09 with Derrick Henry and Saquon Barkley still on the board, all
    # three within a handful of season-long points.
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    board = [
        _player("brown", "RB", name="Chase Brown", vorp=255.2, tier=5),
        _player("henry", "RB", name="Derrick Henry", vorp=246.9, tier=6),
        _player("barkley", "RB", name="Saquon Barkley", vorp=246.7, tier=6),
    ]
    assistant = DraftAssistant(FakeDraftClient([[]]), cache, "draft1", board, ROSTER_POSITIONS, my_draft_slot=1)

    rec = assistant.recommend()

    assert rec.player.player_id == "brown"
    note = next(r for r in rec.reasons if "coin flip" in r)
    assert "Derrick Henry" in note and "Saquon Barkley" in note
    # Must land directly under the headline, not buried at the bottom.
    assert rec.reasons.index(note) == 1


def test_recommend_omits_coin_flip_note_when_the_pick_is_clearly_best(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    board = [
        _player("gibbs", "RB", name="Jahmyr Gibbs", vorp=331.4, tier=1),
        _player("scrub", "RB", name="Deep Bench Back", vorp=180.0, tier=9),
    ]
    assistant = DraftAssistant(FakeDraftClient([[]]), cache, "draft1", board, ROSTER_POSITIONS, my_draft_slot=1)

    assert not any("coin flip" in r for r in assistant.recommend().reasons)


def test_recommend_coin_flip_note_summarises_beyond_three_alternatives(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    board = [_player(f"rb{i}", "RB", name=f"Back {i}", vorp=250.0 - i, tier=5) for i in range(6)]
    assistant = DraftAssistant(FakeDraftClient([[]]), cache, "draft1", board, ROSTER_POSITIONS, my_draft_slot=1)

    note = next(r for r in assistant.recommend().reasons if "coin flip" in r)

    assert "Back 1, Back 2, Back 3 and 2 others" in note
    assert "Back 4" not in note


def test_recommend_coin_flip_note_ignores_other_positions(tmp_path):
    # A close WR is a different decision (different slot, different plan),
    # already covered by the plan-value edge line -- not a coin flip.
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    board = [
        _player("rb1", "RB", name="Lead Back", vorp=250.0, tier=3),
        _player("wr1", "WR", name="Close Receiver", vorp=249.0, tier=3),
        _player("rb2", "RB", name="Far Back", vorp=150.0, tier=9),
    ]
    assistant = DraftAssistant(FakeDraftClient([[]]), cache, "draft1", board, ROSTER_POSITIONS, my_draft_slot=1)

    rec = assistant.recommend()

    assert not any("coin flip" in r for r in rec.reasons)


# Enough bodies to fill QB/RB/RB/WR/WR/TE and BOTH flex slots, so anyone
# added on top is genuinely a bench player rather than a flex starter.
FULL_STARTERS = [
    _player("qb1", "QB", vorp=300),
    _player("rb1", "RB", vorp=250), _player("rb2", "RB", vorp=240),
    _player("wr1", "WR", vorp=260), _player("wr2", "WR", vorp=255),
    _player("te1", "TE", vorp=200),
    _player("wr3", "WR", vorp=250), _player("wr4", "WR", vorp=245),  # the two flex
]


def test_roster_value_rewards_cover_at_a_fragile_position(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    starters = list(FULL_STARTERS)
    backup = _player("rb3", "RB", vorp=180)
    assistant = DraftAssistant(FakeDraftClient([[]]), cache, "draft1", starters + [backup],
                               ROSTER_POSITIONS, my_draft_slot=1)

    # Healthy points are identical (the backup never starts); expected
    # points are not, because he covers an RB who will miss games.
    assert optimize_lineup(starters, ROSTER_POSITIONS).total_points == \
        optimize_lineup(starters + [backup], ROSTER_POSITIONS).total_points
    assert assistant._roster_value(starters + [backup]) > assistant._roster_value(starters)


def test_roster_value_gives_redundant_cover_nothing(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    covered = FULL_STARTERS + [_player("te2", "TE", vorp=190)]
    third = _player("te3", "TE", vorp=120)
    assistant = DraftAssistant(FakeDraftClient([[]]), cache, "draft1", covered + [third],
                               ROSTER_POSITIONS, my_draft_slot=1)

    assert assistant._roster_value(covered + [third]) == assistant._roster_value(covered)


def test_wait_cost_is_zero_for_positions_nobody_is_drafting(tmp_path):
    # The DEF/K case: their pool is untouched, so waiting costs nothing --
    # this is what stops them winning ties against real depth.
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    defenses = [_player(f"d{i}", "DEF", vorp=110 - i) for i in range(5)]
    backs = [_player(f"r{i}", "RB", vorp=250 - 20 * i) for i in range(5)]
    assistant = DraftAssistant(FakeDraftClient([[]]), cache, "draft1", defenses + backs,
                               ROSTER_POSITIONS, my_draft_slot=1)
    pools = {"DEF": defenses, "RB": backs}
    rates = {"DEF": 0.0, "RB": 0.3}

    assert assistant._wait_cost(defenses[0], pools, rates, picks_until=10) == 0.0
    assert assistant._wait_cost(backs[0], pools, rates, picks_until=10) > 0.0


def test_gap_to_next_turn_is_nonzero_while_on_the_clock(tmp_path):
    # Regression: _picks_until_my_turn() is 0 whenever you are ON the
    # clock, which is every live recommendation -- it must not be what
    # feeds the scarcity checks, or they silently switch off mid-draft.
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    board = [_player(f"p{i}", "RB", vorp=300 - i) for i in range(60)]
    assistant = DraftAssistant(FakeDraftClient([[]]), cache, "draft1", board,
                               ROSTER_POSITIONS, my_draft_slot=1, total_rosters=10)
    assistant.picks = [_pick(i, f"p{i}") for i in range(1, 21)]  # pick 21 is mine

    picks_until = assistant._picks_until_my_turn()
    offsets = assistant._my_future_pick_offsets(picks_until)

    assert picks_until == 0          # on the clock right now
    assert offsets and offsets[0] > 0  # but the next turn is a real distance away


def test_consensus_swap_refuses_a_large_downgrade(tmp_path):
    # Regression: the swap fired on a 27.6-point downgrade because the two
    # players shared a tier, then cost a SECOND pick when the better player
    # was re-drafted a round later and the first was benched.
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    ours = _player("te_ours", "TE", name="Our Board TE", vorp=163.6, tier=5)
    theirs = _player("te_cons", "TE", name="Consensus TE", vorp=136.0, tier=5)
    consensus = {"TE": [ConsensusEntry(name="Consensus TE", team="XXX", position="TE",
                                       raw_stats={}, player_id="te_cons")]}
    assistant = DraftAssistant(FakeDraftClient([[]]), cache, "draft1", [ours, theirs],
                               ROSTER_POSITIONS, my_draft_slot=1, consensus_top10=consensus)

    rec = assistant.recommend()

    assert rec.player.player_id == "te_ours"
    assert any("isn't in FantasyPros' consensus top 10" in r for r in rec.reasons)


def test_consensus_swap_still_fires_when_the_alternative_is_close(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    ours = _player("te_ours", "TE", name="Our Board TE", vorp=163.6, tier=5)
    theirs = _player("te_cons", "TE", name="Consensus TE", vorp=160.0, tier=5)
    consensus = {"TE": [ConsensusEntry(name="Consensus TE", team="XXX", position="TE",
                                       raw_stats={}, player_id="te_cons")]}
    assistant = DraftAssistant(FakeDraftClient([[]]), cache, "draft1", [ours, theirs],
                               ROSTER_POSITIONS, my_draft_slot=1, consensus_top10=consensus)

    assert assistant.recommend().player.player_id == "te_cons"


def test_near_tied_plans_defer_the_position_nobody_else_is_drafting(tmp_path):
    # A defense whose plan value is a couple of points ahead must not beat
    # real depth: that margin is inside the noise band, and DEF costs
    # nothing to wait on. This is the round-9-defense case.
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    board = (
        [_player(f"rb{i}", "RB", vorp=250 - 18 * i) for i in range(8)]
        + [_player(f"wr{i}", "WR", vorp=245 - 18 * i) for i in range(8)]
        + [_player(f"d{i}", "DEF", vorp=110 - i) for i in range(8)]
        + [_player(f"k{i}", "K", vorp=80 - i) for i in range(8)]
        + [_player(f"q{i}", "QB", vorp=300 - 5 * i) for i in range(8)]
        + [_player(f"t{i}", "TE", vorp=200 - 15 * i) for i in range(8)]
    )
    assistant = DraftAssistant(FakeDraftClient([[]]), cache, "draft1", board,
                               ROSTER_POSITIONS, my_draft_slot=1, total_rosters=10)
    assistant.picks = [_pick(i, board[i].player_id) for i in range(1, 30)]

    assert assistant.recommend().player.position != "DEF"
