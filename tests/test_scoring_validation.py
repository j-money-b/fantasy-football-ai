"""Validates scoring.score_player against real 2025 actual-stats data (PRD 8,
Phase 1: "replay 2025 actuals through the engine and confirm it reproduces
known real point totals"). Ground truth is Sleeper's own documented
stats endpoint, which returns precomputed pts_std/pts_half_ppr/pts_ppr
alongside the raw stats -- computed independently of our scoring code, so
it's a genuine check rather than circular.

Only the `rec` (points-per-reception) weight differs between standard,
half-PPR, and PPR scoring -- confirmed by hand against real Sleeper league
settings during the Milestone 0 spike (fixtures/sandbox_league_scoring_settings.json
is PPR; varying only `rec` reproduces pts_std/pts_half_ppr/pts_ppr exactly
for every player in the fixture below).
"""

import json
from pathlib import Path

import pytest

from ffai.scoring import score_player

FIXTURES = Path(__file__).parent / "fixtures"

PPR_SETTINGS = json.loads((FIXTURES / "sandbox_league_scoring_settings.json").read_text())
STATS_2025_WK5 = json.loads((FIXTURES / "sleeper_stats_2025_wk5_sample.json").read_text())

EPSILON = 0.05


def _settings_with_rec(rec_value):
    settings = dict(PPR_SETTINGS)
    settings["rec"] = rec_value
    return settings


@pytest.mark.parametrize("player_id,raw_stats", STATS_2025_WK5.items())
def test_score_player_matches_sleeper_ppr(player_id, raw_stats):
    computed = score_player(raw_stats, _settings_with_rec(1.0))
    assert abs(computed - raw_stats["pts_ppr"]) < EPSILON


@pytest.mark.parametrize("player_id,raw_stats", STATS_2025_WK5.items())
def test_score_player_matches_sleeper_half_ppr(player_id, raw_stats):
    computed = score_player(raw_stats, _settings_with_rec(0.5))
    assert abs(computed - raw_stats["pts_half_ppr"]) < EPSILON


@pytest.mark.parametrize("player_id,raw_stats", STATS_2025_WK5.items())
def test_score_player_matches_sleeper_standard(player_id, raw_stats):
    computed = score_player(raw_stats, _settings_with_rec(0.0))
    assert abs(computed - raw_stats["pts_std"]) < EPSILON
