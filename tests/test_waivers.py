from ffai.models import PlayerProjection, WaiverTarget
from ffai.waivers import (
    budget_pace_warning,
    compute_free_agents,
    faab_bid_ranges,
    format_waiver_report,
    is_faab,
    priority_judgment,
    rank_waiver_targets,
    waiver_system_label,
)


def _player(player_id, position, points, team="KC"):
    return PlayerProjection(
        player_id=player_id, name=f"{position}-{player_id}", position=position, team=team,
        bye_week=None, points=points, data_source="test",
    )


def _target(player_id, position, marginal_value, points=0.0):
    return WaiverTarget(player=_player(player_id, position, points), marginal_value=marginal_value, trending_count=None, reasons=[])


# --- waiver_system_label / is_faab -----------------------------------------

def test_waiver_system_label_maps_known_values():
    assert waiver_system_label({"waiver_type": 0}) == "Rolling Waivers"
    assert waiver_system_label({"waiver_type": 1}) == "Reverse Standings"
    assert waiver_system_label({"waiver_type": 2}) == "FAAB Bidding"


def test_waiver_system_label_unknown_value_does_not_guess():
    assert "Unknown" in waiver_system_label({"waiver_type": 99})
    assert "Unknown" in waiver_system_label({})


def test_is_faab_true_only_for_faab_type():
    assert is_faab({"waiver_type": 2}) is True
    assert is_faab({"waiver_type": 0}) is False
    assert is_faab({}) is False


# --- compute_free_agents ------------------------------------------------

class FakeProjections:
    raw_stats_by_player = {}
    source = "test"


def test_compute_free_agents_excludes_rostered_players():
    players_raw = {
        "1": {"position": "QB", "team": "KC", "active": True, "full_name": "Rostered QB"},
        "2": {"position": "RB", "team": "SF", "active": True, "full_name": "Free Agent RB"},
    }
    rosters = [{"roster_id": 1, "players": ["1"]}]

    free_agents = compute_free_agents(players_raw, rosters, FakeProjections(), scoring_settings={})

    assert {p.player_id for p in free_agents} == {"2"}


def test_compute_free_agents_excludes_non_draftable():
    players_raw = {
        "1": {"position": "RB", "team": None, "active": True, "full_name": "No Team"},  # not draftable
        "2": {"position": "RB", "team": "SF", "active": True, "full_name": "Free Agent RB"},
    }

    free_agents = compute_free_agents(players_raw, rosters=[], projections=FakeProjections(), scoring_settings={})

    assert {p.player_id for p in free_agents} == {"2"}


# --- rank_waiver_targets --------------------------------------------------

def test_rank_waiver_targets_fills_empty_slot_for_positive_marginal_value():
    # One empty RB slot -- any real RB free agent should show a positive marginal value.
    roster_positions = ["RB", "BN"]
    my_players = []
    free_agents = [_player("fa1", "RB", 10.0)]

    targets = rank_waiver_targets(free_agents, my_players, roster_positions)

    assert targets[0].player.player_id == "fa1"
    assert targets[0].marginal_value == 10.0
    assert "would add" in targets[0].reasons[0]


def test_rank_waiver_targets_redundant_player_scores_near_zero():
    # Already have a stud RB starting; a lesser RB free agent shouldn't crack the lineup.
    roster_positions = ["RB", "BN"]
    my_players = [_player("mine", "RB", 25.0)]
    free_agents = [_player("fa1", "RB", 5.0)]

    targets = rank_waiver_targets(free_agents, my_players, roster_positions)

    assert targets[0].marginal_value == 0.0
    assert "would not crack" in targets[0].reasons[0]


def test_rank_waiver_targets_degraded_ranks_by_roster_need_then_trending():
    # No QB rostered at all, one dedicated QB slot required -- QB is "needy".
    roster_positions = ["QB", "RB", "BN"]
    my_players = [_player("mine", "RB", 0.0)]
    free_agents = [_player("qb1", "QB", 0.0), _player("rb1", "RB", 0.0)]
    trending_counts = {"rb1": 500}  # trending hard, but not a needed position

    targets = rank_waiver_targets(
        free_agents, my_players, roster_positions,
        trending_counts=trending_counts, projections_degraded=True,
    )

    assert targets[0].player.player_id == "qb1"
    assert "roster need" in targets[0].reasons[0]
    assert all(t.marginal_value == 0.0 for t in targets)


def test_rank_waiver_targets_degraded_falls_back_to_trending_when_no_need():
    roster_positions = ["RB", "BN"]
    my_players = [_player("mine", "RB", 0.0)]  # RB slot already filled -- no roster need
    free_agents = [_player("rb1", "RB", 0.0), _player("rb2", "RB", 0.0)]
    trending_counts = {"rb2": 100}

    targets = rank_waiver_targets(
        free_agents, my_players, roster_positions,
        trending_counts=trending_counts, projections_degraded=True,
    )

    assert targets[0].player.player_id == "rb2"


def test_rank_waiver_targets_respects_limit():
    roster_positions = ["RB", "BN"]
    free_agents = [_player(f"fa{i}", "RB", float(i)) for i in range(20)]

    targets = rank_waiver_targets(free_agents, my_players=[], roster_positions=roster_positions, limit=5)

    assert len(targets) == 5


# --- faab_bid_ranges -------------------------------------------------------

def test_faab_bid_ranges_tiers_by_gap_and_scales_with_budget():
    targets = [
        _target("elite", "RB", 50.0),
        _target("mid", "RB", 10.0),
        _target("scrub", "RB", 9.0),
        _target("extra", "RB", 8.0),
        _target("no_value", "RB", 0.0),
    ]

    bids = faab_bid_ranges(targets, remaining_budget=100)

    assert "no_value" not in bids  # zero marginal value gets no bid recommendation
    elite_low, elite_high = bids["elite"]
    mid_low, mid_high = bids["mid"]
    assert elite_low > mid_low and elite_high > mid_high  # top tier bids more than lower tiers
    assert bids["mid"] == bids["scrub"]  # small gap -- same tier


def test_faab_bid_ranges_empty_when_nothing_has_positive_value():
    targets = [_target("a", "RB", 0.0), _target("b", "WR", -1.0)]

    assert faab_bid_ranges(targets, remaining_budget=100) == {}


# --- budget_pace_warning ---------------------------------------------------

def test_budget_pace_warning_flags_overspending():
    league_settings = {"waiver_budget": 100, "playoff_week_start": 15}
    roster_settings = {"waiver_budget_used": 80}  # 80% spent

    warning = budget_pace_warning(league_settings, roster_settings, current_week=3)  # ~14% through season

    assert warning is not None
    assert "aggressive" in warning


def test_budget_pace_warning_flags_conservatism_late_season():
    league_settings = {"waiver_budget": 100, "playoff_week_start": 15}
    roster_settings = {"waiver_budget_used": 5}  # barely spent

    warning = budget_pace_warning(league_settings, roster_settings, current_week=12)  # ~79% through season

    assert warning is not None
    assert "waste" in warning


def test_budget_pace_warning_none_when_on_pace():
    league_settings = {"waiver_budget": 100, "playoff_week_start": 15}
    roster_settings = {"waiver_budget_used": 50}

    warning = budget_pace_warning(league_settings, roster_settings, current_week=8)

    assert warning is None


def test_budget_pace_warning_none_without_budget_configured():
    assert budget_pace_warning({}, {"waiver_budget_used": 5}, current_week=8) is None


# --- priority_judgment -----------------------------------------------------

def test_priority_judgment_recommends_using_priority_for_strong_target():
    target = _target("fa1", "RB", 10.0)

    note = priority_judgment(target, waiver_position=3)

    assert "worth using your priority" in note
    assert "#3" in note


def test_priority_judgment_recommends_holding_for_weak_or_no_target():
    assert "hold it" in priority_judgment(None)
    assert "hold it" in priority_judgment(_target("fa1", "RB", 0.5))


# --- format_waiver_report ---------------------------------------------------

def test_format_waiver_report_faab_shows_bids_and_budget():
    targets = [_target("fa1", "RB", 10.0)]
    bids = {"fa1": (15, 30)}

    text = format_waiver_report(targets, "FAAB Bidding", faab=True, remaining_budget=70, faab_bids=bids, pace_warning="test pace")

    assert "bid $15-$30" in text
    assert "Remaining budget: $70" in text
    assert "BUDGET: test pace" in text


def test_format_waiver_report_rolling_shows_priority_note():
    targets = [_target("fa1", "RB", 10.0)]

    text = format_waiver_report(targets, "Rolling Waivers", faab=False, priority_note="hold it.")

    assert "hold it." in text
    assert "$" not in text


def test_format_waiver_report_no_targets():
    text = format_waiver_report([], "Rolling Waivers", faab=False)

    assert "No free agents found." in text
