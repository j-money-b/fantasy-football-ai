from ffai.availability import apply_availability
from ffai.brief_model import recommend_lineup
from ffai.models import PlayerProjection


def _player(player_id, position, points, bye_week=None):
    return PlayerProjection(
        player_id=player_id, name=f"{position}-{player_id}", position=position, team="KC",
        bye_week=bye_week, points=points, data_source="test",
    )


def test_ruled_out_player_is_worth_nothing_this_week():
    # The Week 8 2026 failure: Sleeper still projected Egbuka (Out,
    # concussion) at 11.7, so he was started over a healthy bench WR.
    raw = {"1": {"injury_status": "Out", "injury_body_part": "Concussion"}}

    [player] = apply_availability([_player("1", "WR", 11.7)], raw, week=8)

    assert player.points == 0.0
    assert player.unavailable == "Out (Concussion)"
    assert player.healthy_points == 11.7


def test_doubtful_counts_as_not_playing():
    [player] = apply_availability([_player("1", "WR", 11.7)], {"1": {"injury_status": "Doubtful"}}, week=8)

    assert player.points == 0.0


def test_questionable_player_keeps_his_projection():
    [player] = apply_availability([_player("1", "WR", 11.7)], {"1": {"injury_status": "Questionable"}}, week=8)

    assert player.points == 11.7
    assert player.unavailable is None


def test_bye_week_is_unavailable():
    [player] = apply_availability([_player("1", "K", 7.0, bye_week=8)], {}, week=8)

    assert player.points == 0.0
    assert player.unavailable == "on bye"


def test_already_played_free_agent_is_worth_nothing_this_week():
    [player] = apply_availability([_player("1", "WR", 11.7)], {}, week=8, already_played={"1"})

    assert player.points == 0.0
    assert player.unavailable == "already played this week"


def test_optimizer_starts_the_healthy_player_once_availability_is_applied():
    raw = {"1": {"injury_status": "Out"}}
    players = apply_availability([_player("1", "WR", 11.7), _player("2", "WR", 10.3)], raw, week=8)

    lineup, _ = recommend_lineup(players, ["WR", "BN"], ["1"], already_played=set())

    assert lineup.slots[0].player.player_id == "2"


# --- games already played --------------------------------------------------

def test_starter_whose_game_is_over_stays_locked_in_his_slot():
    # Saturday brief, Thursday-night starter already played a dud: he can't be
    # swapped out, however much better the bench option projects.
    players = [_player("1", "WR", 2.0), _player("2", "WR", 15.0)]

    lineup, locked = recommend_lineup(players, ["WR", "BN"], ["1"], already_played={"1"})

    assert lineup.slots[0].player.player_id == "1"
    assert [p.player_id for p in locked] == ["1"]


def test_benched_player_whose_game_is_over_cannot_be_moved_in():
    players = [_player("1", "WR", 20.0), _player("2", "WR", 8.0)]

    lineup, _ = recommend_lineup(players, ["WR", "BN"], ["2"], already_played={"1"})

    assert lineup.slots[0].player.player_id == "2"
