from ffai.roster import find_display_name, find_my_roster, find_opponent_roster


def test_find_my_roster_matches_by_owner_id():
    rosters = [{"roster_id": 1, "owner_id": "u1"}, {"roster_id": 2, "owner_id": "u2"}]

    assert find_my_roster(rosters, "u2") == {"roster_id": 2, "owner_id": "u2"}


def test_find_my_roster_returns_none_when_owner_not_found():
    rosters = [{"roster_id": 1, "owner_id": "u1"}]

    assert find_my_roster(rosters, "missing") is None


def test_find_opponent_roster_matches_by_shared_matchup_id():
    matchups = [
        {"roster_id": 1, "matchup_id": 5},
        {"roster_id": 2, "matchup_id": 5},
        {"roster_id": 3, "matchup_id": 6},
    ]
    rosters = [{"roster_id": 2, "owner_id": "u2"}, {"roster_id": 3, "owner_id": "u3"}]

    assert find_opponent_roster(matchups, my_roster_id=1, rosters=rosters) == {"roster_id": 2, "owner_id": "u2"}


def test_find_opponent_roster_returns_none_when_no_matchup_data():
    # Preseason / before the league's schedule starts -- degrade, don't crash.
    assert find_opponent_roster(matchups=[], my_roster_id=1, rosters=[]) is None


def test_find_opponent_roster_returns_none_when_matchup_id_missing():
    matchups = [{"roster_id": 1, "matchup_id": None}]

    assert find_opponent_roster(matchups, my_roster_id=1, rosters=[]) is None


def test_find_display_name_matches_by_user_id():
    users = [{"user_id": "u1", "display_name": "Alice"}]

    assert find_display_name(users, "u1") == "Alice"


def test_find_display_name_falls_back_to_raw_id_when_user_not_found():
    assert find_display_name([], "u404") == "u404"
