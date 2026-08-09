from ffai.scoring import score_player, score_players


def test_score_player_sums_matching_stats():
    raw_stats = {"rec": 4.0, "rec_yd": 30.0, "rush_yd": 41.0, "rush_td": 2.0}
    scoring_settings = {"rec": 1.0, "rec_yd": 0.1, "rush_yd": 0.1, "rush_td": 6.0}

    assert score_player(raw_stats, scoring_settings) == 23.1


def test_score_player_missing_stat_defaults_to_zero():
    raw_stats = {"pass_yd": 300.0}
    scoring_settings = {"pass_yd": 0.04, "pass_td": 4.0}

    assert score_player(raw_stats, scoring_settings) == 12.0


def test_score_player_ignores_stats_not_in_scoring_settings():
    raw_stats = {"rec": 5.0, "bonus_rec_wr": 5.0, "rec_yd": 50.0}
    scoring_settings = {"rec_yd": 0.1}

    assert score_player(raw_stats, scoring_settings) == 5.0


def test_score_players_bulk_wrapper():
    raw_stats_by_player = {
        "1": {"rush_yd": 100.0},
        "2": {"rush_yd": 50.0},
    }
    scoring_settings = {"rush_yd": 0.1}

    assert score_players(raw_stats_by_player, scoring_settings) == {"1": 10.0, "2": 5.0}
