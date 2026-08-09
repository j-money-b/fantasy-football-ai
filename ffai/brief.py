from datetime import datetime, timezone

from ffai.lineup import optimize_lineup

INJURY_STATUSES_WORTH_FLAGGING = {"Questionable", "Doubtful", "Out", "IR", "PUP", "Suspended"}


def render_brief_markdown(context):
    """Full weekly brief (PRD 6.2 + reliability requirements R1/R2/R6).
    `context` is a weekly_context.WeeklyContext -- this function only
    renders; all data gathering already happened in gather_weekly_context."""
    lines = [f"# Weekly Brief -- Week {context.week}", ""]
    lines.append(f"Last successful run: {datetime.now(timezone.utc).isoformat()}")  # R6

    if context.warnings:  # R1: unmissable, accurate staleness/degradation banner
        lines.append("")
        lines.append("> [!WARNING]")
        lines.append("> DATA MAY BE STALE OR INCOMPLETE -- DO NOT FULLY TRUST THIS BRIEF")
        for warning in context.warnings:
            lines.append(f"> - {warning}")

    lines.append("")
    lines.append("## Your Lineup")
    lines.extend(_render_my_lineup_section(context))

    lines.append("")
    lines.append(f"## Opponent (Week {context.week})")
    lines.extend(_render_opponent_section(context))

    return "\n".join(lines)


def _render_my_lineup_section(context):
    if context.my_roster is None:
        return ["No roster found for you in this league yet."]

    if context.projections_degraded:
        return ["**NO PROJECTIONS AVAILABLE** -- showing your currently-set Sleeper lineup only, not optimized."] + [
            f"  - {line}" for line in _current_starters_lines(context.my_roster, context.my_players)
        ]

    lines = []
    result = optimize_lineup(context.my_players, context.roster_positions)
    lines.append(f"Recommended lineup ({result.total_points:.1f} pts):")
    for slot in result.slots:
        lines.append(f"  - {_slot_line(slot, context.players_raw)}")

    current_starter_ids = {pid for pid in context.my_roster.get("starters") or [] if pid != "0"}
    recommended_ids = {s.player.player_id for s in result.slots if s.player is not None}
    if current_starter_ids and current_starter_ids != recommended_ids:
        swapped_in = recommended_ids - current_starter_ids
        swapped_out = current_starter_ids - recommended_ids
        if swapped_in or swapped_out:
            lines.append("  - Changes vs. your currently-set lineup:")
            by_id = {p.player_id: p for p in context.my_players}
            for player_id in swapped_in:
                p = by_id.get(player_id)
                if p:
                    lines.append(f"    - Start {p.name} ({p.position})")
            for player_id in swapped_out:
                p = by_id.get(player_id)
                if p:
                    lines.append(f"    - Sit {p.name} ({p.position})")

    if result.bench:
        lines.append("Bench:")
        for p in result.bench:
            lines.append(f"  - {_player_line(p, context.players_raw)}")

    return lines


def _render_opponent_section(context):
    if context.opponent_roster is None:
        return ["No matchup data available yet."]

    lines = [f"Facing **{context.opponent_display_name}**."]
    lines.append("Their currently-set starters:")
    lines.extend(f"  - {line}" for line in _current_starters_lines(context.opponent_roster, context.opponent_players))
    return lines


def _current_starters_lines(roster, players):
    by_id = {p.player_id: p for p in players}
    starter_ids = [pid for pid in roster.get("starters") or [] if pid != "0"]
    if not starter_ids:
        return ["(no lineup set yet)"]
    lines = []
    for player_id in starter_ids:
        p = by_id.get(player_id)
        lines.append(_player_line(p, {}) if p else f"(unknown player {player_id})")
    return lines


def _slot_line(slot, players_raw):
    if slot.player is None:
        return f"{slot.slot}: (empty)"
    return f"{slot.slot}: {_player_line(slot.player, players_raw)}"


def _player_line(player, players_raw):
    flag = _injury_flag(player.player_id, players_raw)
    depth = _depth_chart_note(player.player_id, players_raw)
    extras = "".join(part for part in (flag, depth) if part)
    return f"{player.name} ({player.position}, {player.team}) -- {player.points:.1f} pts{extras}"


def _injury_flag(player_id, players_raw):
    status = (players_raw.get(player_id) or {}).get("injury_status")
    return f" [{status}]" if status in INJURY_STATUSES_WORTH_FLAGGING else ""


def _depth_chart_note(player_id, players_raw):
    player = players_raw.get(player_id) or {}
    position = player.get("depth_chart_position")
    order = player.get("depth_chart_order")
    if position and order and order > 1:
        return f" [depth: {position}{order}]"
    return ""
