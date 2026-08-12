def find_my_roster(rosters, user_id):
    """The roster owned by user_id, or None if not found (e.g. the league
    hasn't assigned an owner to any roster yet)."""
    return next((r for r in rosters if r.get("owner_id") == user_id), None)


def find_opponent_roster(matchups, my_roster_id, rosters):
    """The roster facing my_roster_id in this week's matchups, or None if
    there's no matchup data yet (e.g. preseason, before the league's
    schedule starts -- PRD R2: this is a normal degraded-but-not-crashing
    case, not an error) or no opponent is found."""
    my_matchup = next((m for m in matchups if m.get("roster_id") == my_roster_id), None)
    if my_matchup is None or my_matchup.get("matchup_id") is None:
        return None

    opponent_matchup = next(
        (
            m
            for m in matchups
            if m.get("matchup_id") == my_matchup["matchup_id"] and m.get("roster_id") != my_roster_id
        ),
        None,
    )
    if opponent_matchup is None:
        return None

    return next((r for r in rosters if r.get("roster_id") == opponent_matchup.get("roster_id")), None)


def find_roster_by_display_name(rosters, users, display_name):
    """Case-insensitive match against each roster owner's Sleeper display
    name -- lets the trade command name a manager instead of memorizing
    roster_ids. Returns None if no user matches or that user doesn't own a
    roster in this league."""
    user = next((u for u in users if (u.get("display_name") or "").lower() == display_name.lower()), None)
    if user is None:
        return None
    return next((r for r in rosters if r.get("owner_id") == user.get("user_id")), None)


def find_display_name(users, owner_id):
    """Display name for a roster's owner_id, or the raw ID as a fallback if
    the user isn't in the league's /users list (shouldn't normally happen,
    but PRD R2 favors a visible fallback over a crash)."""
    user = next((u for u in users if u.get("user_id") == owner_id), None)
    if user is None:
        return owner_id
    return user.get("display_name") or owner_id
