"""Who can actually play, applied to projections before anything ranks them.

Sleeper's weekly projections ignore injury status entirely: the Week 8 2026
brief had Emeka Egbuka (Out, concussion) at 11.7 pts and Tyler Warren (Out,
groin) at 10.8, so the lineup optimizer started both over healthy bench
players and the brief told the user to "hold" them. Byes were already zeroed
by Sleeper; injuries never were. Every weekly consumer -- lineup, opponent
total, waiver ranking, add/drop -- now reads projections through here, so a
player who will not play is worth 0 everywhere at once."""

from dataclasses import replace

# Will not play this week. Doubtful is included on purpose: those players sit
# far more often than they play, and starting one over a healthy option is
# the exact mistake this module exists to stop.
OUT_THIS_WEEK = {"Out", "Doubtful", "IR", "PUP", "Suspended", "NA", "DNR", "COV"}

# Out for longer than a week. Nothing here knows a return date, so these are
# zeroed across the whole add/drop horizon rather than given an invented one.
# Plain "Out" is often a single week, so future weeks keep their projection
# and the brief says he's Out instead of pretending to know how long.
LONG_TERM = {"IR", "PUP", "Suspended", "NA"}

# Plays, but carries risk worth showing next to the name.
AT_RISK = {"Questionable"}


def status_of(player_id, players_raw):
    return (players_raw.get(player_id) or {}).get("injury_status")


def describe_status(player_id, players_raw):
    """'Out (Concussion)' -- the status plus body part when Sleeper has one."""
    info = players_raw.get(player_id) or {}
    status = info.get("injury_status")
    if not status:
        return None
    part = info.get("injury_body_part")
    return f"{status} ({part})" if part else status


def unavailable_reason(projection, players_raw, week, already_played=frozenset()):
    """Why this player scores nothing this week, or None if he plays."""
    if projection.player_id in already_played:
        # Only meaningful for free agents: adding him now gets you nothing
        # from a game that's already over.
        return "already played this week"
    status = status_of(projection.player_id, players_raw)
    if status in OUT_THIS_WEEK:
        return describe_status(projection.player_id, players_raw)
    if week is not None and projection.bye_week == week:
        return "on bye"
    return None


def apply_availability(projections, players_raw, week, already_played=frozenset()):
    """Copies of `projections` with points zeroed for anyone who won't play
    this week. The original projection is kept on `healthy_points` so the
    brief can still say what he'd have scored."""
    result = []
    for projection in projections:
        reason = unavailable_reason(projection, players_raw, week, already_played)
        if reason is None:
            result.append(projection)
        else:
            result.append(replace(projection, points=0.0, unavailable=reason, healthy_points=projection.points))
    return result


def is_long_term(player_id, players_raw):
    return status_of(player_id, players_raw) in LONG_TERM
