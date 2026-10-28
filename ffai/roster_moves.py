"""Add/drop decisions: who to pick up, who to cut for him, and what to bid.

The old waiver logic asked one question -- "would this free agent start for
you THIS week?" -- and the brief only looked at all when a starter had a
problem. With a full roster that can't recommend anything real: every add is
also a drop, a bench upgrade never showed up at all, and a player on bye this
week looked worthless even if he was the best player on the wire.

So every comparison here is roster against roster over a short horizon (this
week plus the next few, see weekly_context.HORIZON_WEEKS): the value of your
team is the sum of its optimal lineup each week, and a move is worth the
difference. That one number accounts for who actually starts, bye weeks,
injuries, and how deep you already are at the position -- a backup QB who
never starts costs nothing to cut, and the brief can say so."""

from dataclasses import dataclass, field, replace

from ffai.availability import is_long_term
from ffai.lineup import STARTING_SLOT_TYPES, eligible_positions, optimize_lineup
from ffai.scoring import score_player

# A move has to beat this over the whole horizon to be worth a roster spot
# and a transaction. Roughly 1 pt a week over four -- below that the gain is
# inside the week-to-week error of any projection.
MOVE_WORTH_POINTS = 4.0
MAX_MOVES = 3

# How many free agents per position get the full add/drop evaluation, picked
# by raw projected points over the horizon. Anyone below this cut can't beat
# the players above him at the same position, so evaluating him is wasted work.
CANDIDATES_PER_POSITION = 8
WATCH_PER_POSITION = 3

# --- bidding ---------------------------------------------------------------
# A bid has two halves: what the player is worth to YOU, and what it'll take
# to beat the rest of the league to him. The first comes from points; the
# second can only be guessed from how many Sleeper leagues are adding him.
#
# Worth: spread the remaining budget evenly over the remaining weeks, and
# assume a typical week's waiver pickup is worth ~5 pts. That prices a point
# of upgrade at (budget per week / 5). A player who adds 20 pts over the next
# four weeks is worth four weeks of budget.
TYPICAL_WEEKLY_UPGRADE = 5.0
# Never more than half of what's left on one player -- the season is long and
# injuries will create more needs.
MAX_BID_SHARE = 0.5
# (adds across Sleeper in the last day, label, low share of worth, high share)
MARKET_HEAT = [
    (250_000, "heavy competition -- bid near full value", 0.7, 1.0),
    (25_000, "some competition", 0.4, 0.7),
    (0, "little competition", 0.15, 0.35),
]
# Kickers and defenses: there is always another one on the wire next week, so
# real money on one is wasted no matter how good the matchup looks.
STREAMING_POSITIONS = {"K", "DEF"}


@dataclass
class RosterMove:
    add: "PlayerProjection"
    drop: "PlayerProjection"
    gain: float  # lineup points over the horizon
    gain_this_week: float
    weeks: list  # horizon weeks, in order
    add_by_week: list  # add's projected points per horizon week (0 = bye/out)
    drop_by_week: list
    trending_count: "int | None" = None
    bid: "tuple | None" = None  # (low, high) dollars; None outside FAAB
    worth: "int | None" = None  # the most this player is worth to you, dollars
    market: "str | None" = None  # competition label behind the bid
    reasons: list = field(default_factory=list)  # plain-English why, add side then drop side


@dataclass
class WireEntry:
    player: "PlayerProjection"
    horizon_points: float  # raw projected points over the horizon
    gain: float  # best achievable roster gain if added (can be <= 0)
    best_drop: "PlayerProjection | None"
    trending_count: "int | None"


@dataclass
class MovePlan:
    weeks: list
    moves: list  # list[RosterMove], best first, each assuming the earlier ones were made
    wire: dict  # position -> list[WireEntry], best gain first -- the always-on scan
    rising: list  # list[WireEntry] the market is buying hardest, whatever their projection


def future_points(future_projections, scoring_settings):
    """week -> {player_id: points} under this league's scoring."""
    return {
        week: {pid: score_player(stats, scoring_settings) for pid, stats in result.raw_stats_by_player.items()}
        for week, result in sorted(future_projections.items())
    }


class _Horizon:
    """Projected points per player per horizon week, and the roster value
    that follows from them. Week 0 is this week (availability already
    applied upstream); later weeks come from Sleeper's future projections,
    zeroed for anyone out long-term."""

    def __init__(self, week, future_scores, players_raw, roster_positions):
        self.weeks = [week] + list(future_scores)
        self.future_scores = future_scores
        self.players_raw = players_raw
        self.roster_positions = roster_positions
        self._by_week = {}  # player_id -> list[float]
        self._players = {}  # player_id -> PlayerProjection (this week)

    def points(self, player):
        cached = self._by_week.get(player.player_id)
        if cached is not None:
            return cached
        long_term = is_long_term(player.player_id, self.players_raw)
        row = [player.points]
        for week in self.weeks[1:]:
            row.append(0.0 if long_term else round(self.future_scores[week].get(player.player_id, 0.0), 2))
        self._by_week[player.player_id] = row
        self._players[player.player_id] = player
        return row

    def by_week_totals(self, roster):
        totals = []
        for index in range(len(self.weeks)):
            week_players = [replace(p, points=self.points(p)[index]) for p in roster]
            totals.append(optimize_lineup(week_players, self.roster_positions).total_points)
        return totals

    def value(self, roster):
        return sum(self.by_week_totals(roster))


def plan_moves(
    my_players,
    free_agents,
    roster_positions,
    week,
    future_scores,
    players_raw,
    trending_counts=None,
    remaining_budget=None,
    max_moves=MAX_MOVES,
    min_gain=MOVE_WORTH_POINTS,
):
    """Greedy best-move-first: find the single add/drop that improves the
    roster most over the horizon, make it, and look again. Each later move
    therefore assumes the earlier ones happened, so two moves never both
    claim the same drop or the same lineup slot."""
    trending_counts = trending_counts or {}
    horizon = _Horizon(week, future_scores, players_raw, roster_positions)
    candidates = _candidates(free_agents, horizon, trending_counts, roster_positions)

    roster = list(my_players)
    moves = []
    first_pass = None
    added = set()
    for _ in range(max_moves):
        evaluations = _evaluate(roster, [c for c in candidates if c.player_id not in added], horizon)
        if first_pass is None:
            first_pass = evaluations
        best = max(evaluations.values(), key=lambda e: e[0], default=None)
        if best is None or best[0] < min_gain:
            break
        gain, add, drop, gain_this_week = best
        moves.append(_move(add, drop, gain, gain_this_week, horizon, trending_counts, roster, week, remaining_budget))
        roster = [p for p in roster if p.player_id != drop.player_id] + [add]
        added.add(add.player_id)

    wire = _wire(first_pass or {}, horizon, trending_counts)
    rising = _rising(free_agents, first_pass or {}, horizon, trending_counts)
    return MovePlan(weeks=horizon.weeks, moves=moves, wire=wire, rising=rising)


def _candidates(free_agents, horizon, trending_counts, roster_positions):
    positions = set()
    for slot in roster_positions:
        if slot in STARTING_SLOT_TYPES:
            positions |= eligible_positions(slot)

    by_position = {}
    for player in free_agents:
        if player.position not in positions:
            continue
        total = sum(horizon.points(player))
        if total <= 0:
            continue
        by_position.setdefault(player.position, []).append((total, player))

    chosen = {}
    for entries in by_position.values():
        entries.sort(key=lambda e: e[0], reverse=True)
        for _, player in entries[:CANDIDATES_PER_POSITION]:
            chosen[player.player_id] = player
    # Whoever the market is buying gets evaluated too, even below the cut --
    # the brief should be able to say "everyone's adding him, here's why you
    # shouldn't" rather than silently ignoring him.
    for player in free_agents:
        if trending_counts.get(player.player_id) and player.position in positions:
            chosen.setdefault(player.player_id, player)
    return list(chosen.values())


def _evaluate(roster, candidates, horizon):
    """candidate player_id -> (gain, add, best drop, gain this week)."""
    base_totals = horizon.by_week_totals(roster)
    base = sum(base_totals)
    results = {}
    for add in candidates:
        best = None
        for drop in roster:
            if add.position in STREAMING_POSITIONS and drop.position != add.position:
                # Kickers and defenses are streamed one-for-one. Cutting a
                # position player to roster a second one can look good on
                # projections, but it throws away the bench depth that covers
                # an injury or a Questionable starter sitting.
                continue
            new_roster = [p for p in roster if p.player_id != drop.player_id] + [add]
            totals = horizon.by_week_totals(new_roster)
            gain = round(sum(totals) - base, 2)
            if best is None or gain > best[0]:
                best = (gain, add, drop, round(totals[0] - base_totals[0], 2))
        if best is not None:
            results[add.player_id] = best
    return results


def _move(add, drop, gain, gain_this_week, horizon, trending_counts, roster, week, remaining_budget):
    add_by_week = horizon.points(add)
    drop_by_week = horizon.points(drop)
    trending = trending_counts.get(add.player_id)
    move = RosterMove(
        add=add, drop=drop, gain=gain, gain_this_week=gain_this_week, weeks=horizon.weeks,
        add_by_week=add_by_week, drop_by_week=drop_by_week, trending_count=trending,
    )
    if remaining_budget is not None:
        move.bid, move.worth, move.market = bid_range(add, gain, len(horizon.weeks), remaining_budget, week, trending)
    move.reasons = _reasons(move, horizon, roster)
    return move


def bid_range(player, gain, horizon_len, remaining_budget, week, trending=None, last_week=17):
    """(low, high), worth, market label. See the constants above for the
    reasoning; this is a stated heuristic, not a market model -- nothing here
    can see the other managers' budgets or intentions."""
    if remaining_budget is None or remaining_budget <= 0:
        return None, None, None
    if player.position in STREAMING_POSITIONS:
        return (1, 1), 1, "always another one available next week -- never pay real money"

    weeks_left = max(last_week - week + 1, 1)
    per_point = remaining_budget / weeks_left / TYPICAL_WEEKLY_UPGRADE
    worth = min(gain * per_point, remaining_budget * MAX_BID_SHARE)
    worth = max(round(worth), 1)

    count = trending or 0
    for floor, label, low_share, high_share in MARKET_HEAT:
        if count >= floor:
            low = max(round(worth * low_share), 1)
            high = max(round(worth * high_share), low)
            return (low, high), worth, label
    return (1, worth), worth, None


def _reasons(move, horizon, roster):
    reasons = []
    add, drop = move.add, move.drop
    weeks = move.weeks

    if move.gain_this_week > 0:
        reasons.append(f"Improves this week's lineup by {move.gain_this_week:.1f} pts.")
    elif add.unavailable:
        reasons.append(f"{add.name} is {add.unavailable} this week -- this is a pickup for the weeks after.")
    else:
        reasons.append(f"Doesn't change this week's lineup -- the payoff comes in the weeks after.")

    add_weeks = ", ".join(f"wk{w} {p:.1f}" for w, p in zip(weeks, move.add_by_week))
    reasons.append(f"{add.name} projects {sum(move.add_by_week):.1f} pts over weeks {weeks[0]}-{weeks[-1]} ({add_weeks}).")
    if move.trending_count and add.position in STREAMING_POSITIONS:
        reasons.append(f"Added in {move.trending_count:,} Sleeper leagues in the last day -- a popular streaming pick for the matchups ahead.")
    elif move.trending_count:
        reasons.append(f"Added in {move.trending_count:,} Sleeper leagues in the last day -- usually a role change or an injury ahead of him.")

    reasons.append(_drop_reason(drop, move.drop_by_week, weeks, roster, horizon))
    return reasons


def _drop_reason(drop, drop_by_week, weeks, roster, horizon):
    starts = _weeks_started(drop, roster, horizon)
    same_position = sum(1 for p in roster if p.position == drop.position)
    if drop.unavailable and "on bye" not in drop.unavailable and is_long_term(drop.player_id, horizon.players_raw):
        return f"Cut {drop.name}: {drop.unavailable} with no return date -- he can't help you in this window."
    if starts == 0:
        if drop.position in {"QB", "K", "DEF", "TE"} and same_position > 1:
            return (f"Cut {drop.name}: he wouldn't start a single week of the next {len(weeks)} -- "
                    f"you only start one {drop.position}, and the other one is better.")
        return f"Cut {drop.name}: he wouldn't crack your lineup in any of the next {len(weeks)} weeks."
    byes = [w for w, p in zip(weeks, drop_by_week) if p == 0]
    note = f" (zero in week {', '.join(map(str, byes))})" if byes else ""
    return (f"Cut {drop.name}: he'd start {starts} of the next {len(weeks)} weeks, but the new player "
            f"scores more over that stretch{note}.")


def _weeks_started(player, roster, horizon):
    count = 0
    for index in range(len(horizon.weeks)):
        week_players = [replace(p, points=horizon.points(p)[index]) for p in roster]
        lineup = optimize_lineup(week_players, horizon.roster_positions)
        if any(s.player is not None and s.player.player_id == player.player_id and s.player.points > 0
               for s in lineup.slots):
            count += 1
    return count


def _wire(evaluations, horizon, trending_counts):
    by_position = {}
    for gain, add, drop, _ in evaluations.values():
        by_position.setdefault(add.position, []).append(WireEntry(
            player=add, horizon_points=round(sum(horizon.points(add)), 1), gain=gain,
            best_drop=drop if gain > 0 else None, trending_count=trending_counts.get(add.player_id),
        ))
    ordered = {}
    for position in sorted(by_position, key=_position_order):
        entries = sorted(by_position[position], key=lambda e: (e.gain, e.horizon_points), reverse=True)
        ordered[position] = entries[:WATCH_PER_POSITION]
    return ordered


def _rising(free_agents, evaluations, horizon, trending_counts, limit=5):
    trending = [p for p in free_agents if trending_counts.get(p.player_id)]
    trending.sort(key=lambda p: trending_counts[p.player_id], reverse=True)
    entries = []
    for player in trending[:limit]:
        evaluation = evaluations.get(player.player_id)
        gain = evaluation[0] if evaluation else 0.0
        entries.append(WireEntry(
            player=player, horizon_points=round(sum(horizon.points(player)), 1), gain=gain,
            best_drop=evaluation[2] if evaluation and gain > 0 else None,
            trending_count=trending_counts[player.player_id],
        ))
    return entries


def format_move_plan(plan, remaining_budget=None):
    """Plain-text form for the `waivers`/`refresh` commands -- the same
    moves the brief recommends, so the two can't disagree."""
    span = f"{plan.weeks[0]}-{plan.weeks[-1]}" if len(plan.weeks) > 1 else str(plan.weeks[0])
    lines = []
    if remaining_budget is not None:
        lines.append(f"FAAB budget left: ${remaining_budget}")
    if not plan.moves:
        lines.append(f"No add/drop worth making over weeks {span}.")
    for index, move in enumerate(plan.moves, start=1):
        bid = ""
        if move.bid:
            low, high = move.bid
            bid = f" -- bid ${low}" if low == high else f" -- bid ${low}-${high}"
        lines.append(f"{index}. ADD {move.add.name} ({move.add.position}, {move.add.team}), "
                     f"DROP {move.drop.name}{bid} -- +{move.gain:.1f} pts over weeks {span}")
        lines.extend(f"     {reason}" for reason in move.reasons)
    lines.append("")
    lines.append(f"Best available by position (pts over weeks {span}, gain after your best cut):")
    for position, entries in plan.wire.items():
        for entry in entries:
            drop = f" for {entry.best_drop.name}" if entry.best_drop else ""
            flag = f" [{entry.player.unavailable}]" if entry.player.unavailable else ""
            lines.append(f"  {position:3} {entry.player.name} ({entry.player.team}){flag} -- "
                         f"{entry.horizon_points:.1f} pts, gain {entry.gain:+.1f}{drop}")
    return "\n".join(lines)


POSITION_ORDER = ["QB", "RB", "WR", "TE", "K", "DEF"]


def _position_order(position):
    return POSITION_ORDER.index(position) if position in POSITION_ORDER else len(POSITION_ORDER)
