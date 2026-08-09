from ffai.cache import Cache
from ffai.draft_assistant import DraftAssistant, format_recommendation
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


def test_recommend_picks_best_available_by_vorp_with_gap_and_tier_reasoning(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    board = [_player("a", "RB", vorp=20, tier=1), _player("b", "WR", vorp=12, tier=2)]
    assistant = DraftAssistant(FakeDraftClient([[]]), cache, "draft1", board, ROSTER_POSITIONS, my_draft_slot=1)

    rec = assistant.recommend()

    assert rec.player.player_id == "a"
    assert rec.degraded is False
    assert any("VORP" in r and "8.0 ahead" in r for r in rec.reasons)
    assert any("Tier 1" in r for r in rec.reasons)
    assert any("no RB rostered yet" in r for r in rec.reasons)


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
