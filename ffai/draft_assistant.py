import logging
from collections import Counter

from ffai.models import Recommendation
from ffai.repository import fetch_draft_picks
from ffai.sleeper_client import SleeperAPIError
from ffai.vorp import DEDICATED_POSITIONS

logger = logging.getLogger(__name__)

POSITIONAL_RUN_WINDOW = 5
POSITIONAL_RUN_THRESHOLD = 3


class DraftAssistant:
    """Polls a Sleeper draft's picks feed, tracks board state, and
    recommends the best available pick with reasoning. `board` is the full
    pre-built big board (list[PlayerVorp], VORP-sorted descending) --
    everything here only reasons over that flat list, never touching
    Sleeper/projections payload shapes directly (those live in
    player_pool.py / projections.py)."""

    def __init__(
        self,
        client,
        cache,
        draft_id,
        board,
        roster_positions,
        my_draft_slot,
        board_degraded=False,
        consensus_top10=None,
    ):
        self.client = client
        self.cache = cache
        self.draft_id = draft_id
        self.board = board
        self.roster_positions = roster_positions
        self.my_draft_slot = str(my_draft_slot)
        self.board_degraded = board_degraded
        self.consensus_top10 = consensus_top10 or {}

        self.picks = []  # raw pick dicts from Sleeper, in pick_no order
        self.drafted_player_ids = set()
        self.my_drafted_players = []  # list[PlayerVorp]
        self._by_player_id = {p.player_id: p for p in board}

    def poll_once(self):
        """Fetches the current picks feed and returns only the picks new
        since the last poll (R2: a fetch failure is swallowed and logged,
        not raised -- the loop keeps going on stale in-memory state).

        "My" picks are matched via draft_slot, not roster_id -- verified
        empirically against a real Sleeper mock draft, where every pick's
        roster_id came back null and only draft_slot was reliably populated.
        draft_slot works the same way for mock and real drafts."""
        try:
            picks, _stale, _fetched_at = fetch_draft_picks(self.client, self.cache, self.draft_id)
        except SleeperAPIError as exc:
            logger.warning("draft picks fetch failed, keeping prior state: %s", exc)
            return []

        if picks[: len(self.picks)] == self.picks:
            new_picks = picks[len(self.picks):]
        else:
            # Picks feed is expected append-only by pick_no; if the
            # previously-seen prefix changed unexpectedly, fall back to
            # diffing by the set of pick_no values rather than trusting index
            # order.
            logger.warning("draft picks feed prefix changed unexpectedly; re-diffing by pick_no")
            seen_pick_nos = {p["pick_no"] for p in self.picks}
            new_picks = [p for p in picks if p["pick_no"] not in seen_pick_nos]

        self.picks = picks
        for pick in new_picks:
            player_id = pick.get("player_id")
            if not player_id:
                continue
            self.drafted_player_ids.add(player_id)
            if str(pick.get("draft_slot")) == self.my_draft_slot:
                matched = self._by_player_id.get(player_id)
                if matched:
                    self.my_drafted_players.append(matched)

        return new_picks

    def available_players(self):
        return [p for p in self.board if p.player_id not in self.drafted_player_ids]

    def detect_positional_run(self, window=POSITIONAL_RUN_WINDOW, threshold=POSITIONAL_RUN_THRESHOLD):
        recent_positions = []
        for pick in self.picks[-window:]:
            player = self._by_player_id.get(pick.get("player_id"))
            if player:
                recent_positions.append(player.position)
        if not recent_positions:
            return None
        position, count = Counter(recent_positions).most_common(1)[0]
        return position if count >= threshold else None

    def _needed_positions(self):
        """Dedicated (non-flex, non-bench) position slots not yet filled by
        my_drafted_players. A coarse "still need" signal -- doesn't attempt
        to model flex-slot allocation the way vorp.compute_replacement_levels
        does; that level of precision isn't needed for a reasoning nudge."""
        slot_counts = Counter(pos for pos in self.roster_positions if pos in DEDICATED_POSITIONS)
        filled_counts = Counter(p.position for p in self.my_drafted_players)
        return {pos for pos, count in slot_counts.items() if filled_counts[pos] < count}

    def recommend(self):
        available = self.available_players()
        if not available:
            return None

        if self.board_degraded:
            needed = self._needed_positions()
            available = sorted(available, key=lambda p: p.position not in needed)

        best = available[0]
        reasons = []

        if self.board_degraded:
            reasons.append("NO PROJECTIONS AVAILABLE -- ranked by roster need only, not projected points.")
        else:
            next_best = available[1] if len(available) > 1 else None
            if next_best:
                gap = best.vorp - next_best.vorp
                reasons.append(f"Best available by VORP ({best.vorp:+.1f}), {gap:.1f} ahead of {next_best.name}.")
            if best.tier is not None:
                reasons.append(f"Tier {best.tier} at {best.position}.")

        my_positions = Counter(p.position for p in self.my_drafted_players)
        if my_positions[best.position] == 0:
            reasons.append(f"You have no {best.position} rostered yet.")

        if best.bye_week is not None:
            colliding = [
                p for p in self.my_drafted_players if p.position == best.position and p.bye_week == best.bye_week
            ]
            if colliding:
                names = ", ".join(p.name for p in colliding)
                reasons.append(f"Bye-week collision at {best.position} (week {best.bye_week}) with {names}.")

        run_position = self.detect_positional_run()
        if run_position == best.position:
            reasons.append(
                f"{best.position} run in progress "
                f"({POSITIONAL_RUN_THRESHOLD}+ of the last {POSITIONAL_RUN_WINDOW} picks)."
            )

        stack_note = self._stack_note(best)
        if stack_note:
            reasons.append(stack_note)

        if any(e.player_id == best.player_id for e in self.consensus_top10.get(best.position, [])):
            reasons.append(f"Also in FantasyPros' consensus top 10 at {best.position}.")

        return Recommendation(
            player=best, reasons=reasons, degraded=self.board_degraded, data_source=best.data_source
        )

    def _stack_note(self, candidate):
        if candidate.position in ("WR", "TE"):
            my_qb = next((p for p in self.my_drafted_players if p.position == "QB" and p.team == candidate.team), None)
            if my_qb:
                return f"Stacks with your QB {my_qb.name} ({candidate.team})."
        elif candidate.position == "QB":
            my_pass_catcher = next(
                (p for p in self.my_drafted_players if p.position in ("WR", "TE") and p.team == candidate.team), None
            )
            if my_pass_catcher:
                return f"Stacks with your {my_pass_catcher.position} {my_pass_catcher.name} ({candidate.team})."
        return None


def format_recommendation(recommendation):
    if recommendation is None:
        return "No available players remain on the board."
    lines = [f"Recommended pick: {recommendation.player.name} ({recommendation.player.position}, {recommendation.player.team})"]
    for reason in recommendation.reasons:
        lines.append(f"  - {reason}")
    return "\n".join(lines)
