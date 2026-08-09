def score_player(raw_stats: dict, scoring_settings: dict) -> float:
    """Fantasy points for one player: sum of each league scoring rule times
    the matching raw stat (0 if the player's stat line doesn't have that key --
    Sleeper omits zero-valued stats rather than including them explicitly).
    PRD 5: computed from raw stats + the league's own scoring_settings so the
    result is correct for any format (PPR/half/standard/custom) with no
    per-league configuration needed here."""
    total = sum(raw_stats.get(key, 0.0) * weight for key, weight in scoring_settings.items())
    return round(total, 2)


def score_players(raw_stats_by_player: dict, scoring_settings: dict) -> dict:
    """Bulk wrapper over score_player. Players missing from raw_stats_by_player
    are the caller's concern (not raised here) -- score only what's given."""
    return {
        player_id: score_player(raw_stats, scoring_settings)
        for player_id, raw_stats in raw_stats_by_player.items()
    }
