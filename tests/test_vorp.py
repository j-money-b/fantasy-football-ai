from ffai.models import PlayerProjection, PlayerVorp
from ffai.vorp import build_tiers, compute_replacement_levels, compute_vorp


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


def test_compute_replacement_levels_synthetic_4_team_league():
    # 4 teams, roster: QB, RB, RB, WR, WR, FLEX(RB/WR/TE), bench.
    # Hand-computed expected demand:
    #   QB: dedicated 4*1=4
    #   RB: dedicated 4*2=8, + 2 flex (top of the RB/WR/TE remainder pool) = 10
    #   WR: dedicated 4*2=8, + 0 flex = 8
    #   TE: dedicated 0,      + 2 flex = 2
    roster_positions = ["QB", "RB", "RB", "WR", "WR", "FLEX", "BN", "BN"]
    total_rosters = 4

    players = (
        [_player(f"qb{i}", "QB", pts) for i, pts in enumerate([20, 18, 16, 14, 12, 10])]
        + [_player(f"rb{i}", "RB", pts) for i, pts in enumerate([30, 28, 26, 24, 22, 20, 18, 16, 14, 12])]
        + [_player(f"wr{i}", "WR", pts) for i, pts in enumerate([25, 23, 21, 19, 17, 15, 13, 11, 9, 7])]
        + [_player(f"te{i}", "TE", pts) for i, pts in enumerate([15, 13, 11, 9, 7])]
    )

    replacement_levels = compute_replacement_levels(players, roster_positions, total_rosters)

    assert replacement_levels == {"QB": 12, "RB": 12, "WR": 9, "TE": 11}


def test_compute_replacement_levels_demand_exceeding_supply_uses_worst_available():
    # 1 team, 2 DEF slots, only 1 DEF player available -- demand (2) exceeds supply (1).
    roster_positions = ["DEF", "DEF"]
    players = [_player("def1", "DEF", 5.0)]

    replacement_levels = compute_replacement_levels(players, roster_positions, total_rosters=1)

    assert replacement_levels == {"DEF": 5.0}


def test_compute_vorp_sorted_descending_and_subtracts_replacement():
    players = [_player("a", "RB", 20.0), _player("b", "RB", 10.0), _player("c", "WR", 15.0)]
    replacement_levels = {"RB": 8.0, "WR": 5.0}

    result = compute_vorp(players, replacement_levels)

    assert [(p.player_id, p.vorp) for p in result] == [("a", 12.0), ("c", 10.0), ("b", 2.0)]
    assert all(isinstance(p, PlayerVorp) for p in result)


def test_compute_vorp_missing_position_in_replacement_levels_defaults_to_zero():
    players = [_player("a", "K", 10.0)]

    result = compute_vorp(players, replacement_levels={})

    assert result[0].vorp == 10.0


def test_build_tiers_splits_on_large_gaps():
    def _vorp_player(player_id, vorp):
        return PlayerVorp(
            player_id=player_id, name=player_id, position="RB", team="XXX",
            bye_week=None, points=vorp, data_source="test", vorp=vorp,
        )

    # gaps: 2,1,1,17,1,19,1,1 -- two clear breaks after index 2 (50->30) and index 4 (29->10)
    values = [50, 48, 47, 30, 29, 10, 9, 8]
    players = [_vorp_player(f"p{i}", v) for i, v in enumerate(values)]

    tiers = build_tiers(players)

    assert [len(t.players) for t in tiers] == [3, 2, 3]
    assert [p.tier for p in tiers[0].players] == [1, 1, 1]
    assert [p.tier for p in tiers[1].players] == [2, 2]
    assert [p.tier for p in tiers[2].players] == [3, 3, 3]


def test_build_tiers_no_big_gaps_produces_single_tier():
    def _vorp_player(player_id, vorp):
        return PlayerVorp(
            player_id=player_id, name=player_id, position="RB", team="XXX",
            bye_week=None, points=vorp, data_source="test", vorp=vorp,
        )

    players = [_vorp_player(f"p{i}", v) for i, v in enumerate([10, 9, 8, 7, 6])]

    tiers = build_tiers(players)

    assert len(tiers) == 1
    assert len(tiers[0].players) == 5


def test_build_tiers_empty_list():
    assert build_tiers([]) == []
