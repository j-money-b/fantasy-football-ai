from ffai.models import PlayerProjection
from ffai.projections import DEFAULT_POSITIONS
from ffai.scoring import score_player


def is_draftable(player, positions=DEFAULT_POSITIONS):
    """A real, rosterable NFL player at a scored position. Sleeper's
    directory keeps thousands of practice-squad/unrostered entries marked
    active=True with no team assigned (e.g. search_rank: 9999999, its own
    "filler" signal) -- team is required, not just active, or those pollute
    the board with noise the VORP ranking can't distinguish from real
    players. Shared between to_projection_pool (the board) and the CLI's
    known_player_ids (the R4 coverage-check denominator) so the two stay
    consistent."""
    return bool(player.get("position") in positions and player.get("active") and player.get("team"))


def to_projection_pool(players_raw, projections, scoring_settings, byes_by_team=None, positions=DEFAULT_POSITIONS):
    """Composition root: joins the player directory, the projections
    adapter's raw stats, and the league's own scoring rules into the flat
    list downstream VORP/draft-assistant code consumes. Neither of those
    modules needs to know Sleeper/ESPN/FantasyPros payload shapes -- this is
    the only place that does.

    Players with no entry in the projections payload still appear
    (points=0.0, data_source="none") rather than disappearing from the board
    (PRD R2 -- stay visible even in a degraded/gappy data situation)."""
    byes_by_team = byes_by_team or {}
    raw_stats_by_player = projections.raw_stats_by_player

    pool = []
    for player_id, player in players_raw.items():
        if not is_draftable(player, positions):
            continue
        position = player.get("position")

        raw_stats = raw_stats_by_player.get(player_id)
        if raw_stats is None:
            points, data_source = 0.0, "none"
        else:
            points, data_source = score_player(raw_stats, scoring_settings), projections.source

        team = player.get("team")
        name = player.get("full_name") or f"{player.get('first_name', '')} {player.get('last_name', '')}".strip()
        pool.append(
            PlayerProjection(
                player_id=player_id,
                name=name,
                position=position,
                team=team,
                bye_week=byes_by_team.get(team),
                points=points,
                data_source=data_source,
            )
        )
    return pool
