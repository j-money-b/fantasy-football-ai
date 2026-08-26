import logging
from collections import Counter, defaultdict

from ffai.lineup import optimize_lineup
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
    player_pool.py / projections.py).

    In the healthy (non-degraded) path, recommendations still rank by
    VORP -- but VORP alone has no concept of "you already have enough of
    these": a position with a steep drop-off after its top tier (e.g. a
    shallow TE class) computes a very low replacement level, which
    inflates VORP for every player above that line, so pure VORP-ordering
    can get stuck recommending the same position pick after pick even
    once your roster's need there is long since satisfied (confirmed
    empirically: it repeatedly recommended TEs, then a second DEF, well
    past the point either was useful). To fix that, every candidate is
    first gated on marginal lineup value -- would they actually add any
    points to your optimal starting lineup right now (lineup.
    optimize_lineup, same technique waivers.py already uses)? A 3rd/4th TE
    or a 2nd DEF gates to zero once your slots are full and drops behind
    anyone who still has open roster capacity. VORP breaks ties within
    that gate (deliberately NOT raw marginal points as the ranking metric
    itself -- an earlier version of this ranked by raw marginal points
    and got stuck recommending QB for 10+ consecutive rounds instead,
    since QB outscores every other position per game and has no flex
    outlet; VORP is what correctly weighs scarcity across positions, the
    gate is only there to catch "already have enough of these")."""

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
        total_rosters=None,
    ):
        self.client = client
        self.cache = cache
        self.draft_id = draft_id
        self.board = board
        self.roster_positions = roster_positions
        self.my_draft_slot = str(my_draft_slot)
        self.board_degraded = board_degraded
        self.consensus_top10 = consensus_top10 or {}
        self.total_rosters = total_rosters  # optional: enables the "picks until your turn" scarcity check

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
        does; that level of precision isn't needed for a reasoning nudge.
        Only used in the degraded fallback path -- the healthy path uses
        actual marginal lineup value instead, which already accounts for
        flex slots exactly."""
        slot_counts = Counter(pos for pos in self.roster_positions if pos in DEDICATED_POSITIONS)
        filled_counts = Counter(p.position for p in self.my_drafted_players)
        return {pos for pos, count in slot_counts.items() if filled_counts[pos] < count}

    def _position_floor_vorp(self, same_position_ranked, picks_until):
        """The VORP of the worst-case player still available at this
        position by the time it's your next turn, assuming `picks_until`
        other teams each grab one first -- a property of the POSITION as a
        whole, not of any individual candidate (that matters: computing a
        "what's behind ME specifically" floor per-candidate isn't
        monotonic -- the 2nd-best player in a tier can look behind a
        deeper, more-negative floor than the 1st-best one and end up with
        a bigger bonus, which would rank a clearly-worse player above a
        clearly-better one at the same position). 0.0 if the position
        could be completely exhausted by then."""
        if picks_until is None:
            return None
        return same_position_ranked[picks_until].vorp if picks_until < len(same_position_ranked) else 0.0

    def _cliff_adjusted_vorp(self, player, floor_vorp):
        """player.vorp, boosted by how much value is at risk of vanishing
        at this position before your next turn (see _position_floor_vorp).
        A small, steady decline (e.g. mid-tier QB in a deep class) leaves
        the floor close to the top of the position, so it earns a small
        bonus; a real cliff (e.g. the last two usable RBs before a 30+
        point crater) leaves a much lower floor and earns a big one --
        deliberately NOT based on vorp.build_tiers' gap clustering, which
        splits smoothly-declining positions (QB, WR) into many 1-2-player
        tiers and made ordinary decline look like a cliff.

        Restricted to player.vorp > 0 -- already-below-replacement players
        never get an urgency boost. A deep bench tail (backup kickers
        projected near zero points, e.g.) can pull the floor very negative,
        producing a large bonus for a player nobody would actually reach
        for; below replacement is below replacement no matter how much
        worse the tail behind it gets."""
        if floor_vorp is None or player.vorp <= 0:
            return player.vorp

        return player.vorp + max(0.0, player.vorp - floor_vorp)

    def _picks_until_my_turn(self):
        """Snake-draft distance, in picks, from right now until my
        draft_slot is next on the clock. Returns None if total_rosters
        wasn't supplied -- this feature degrades off rather than guessing
        (PRD anti-goal: confidently wrong)."""
        if not self.total_rosters:
            return None
        my_slot = int(self.my_draft_slot)
        pick_no = len(self.picks) + 1
        distance = 0
        while True:
            round_no = (pick_no - 1) // self.total_rosters + 1
            pos_in_round = (pick_no - 1) % self.total_rosters + 1
            slot_for_pick = pos_in_round if round_no % 2 == 1 else self.total_rosters - pos_in_round + 1
            if slot_for_pick == my_slot:
                return distance
            pick_no += 1
            distance += 1

    def _consensus_backed(self, player):
        """True if the player is in FantasyPros' consensus top 10 at their
        position, OR if we have no consensus data at all for that position
        (fetch failure, or a position FantasyPros' free tier doesn't cover
        like K/DEF) -- absence of data isn't evidence our own number is an
        outlier, so it isn't treated as a red flag. Used to decide whether
        the TOP pick is suspicious. Deliberately NOT used to vet a
        replacement candidate -- see _has_real_consensus_backing."""
        entries = self.consensus_top10.get(player.position)
        if not entries:
            return True
        return any(e.player_id == player.player_id for e in entries)

    def _has_real_consensus_backing(self, player):
        """True only if we have an actual FantasyPros top-10 list for this
        position AND this specific player is on it -- no benefit-of-the-
        doubt default. Used to vet a REPLACEMENT candidate: "no data" must
        never count as a reason to prefer swapping to this player, or a
        position FantasyPros doesn't cover (K/DEF) would look like a safe
        harbor for every dampened pick regardless of position."""
        entries = self.consensus_top10.get(player.position)
        return bool(entries) and any(e.player_id == player.player_id for e in entries)

    def recommend(self):
        available = self.available_players()
        if not available:
            return None

        if self.board_degraded:
            return self._recommend_degraded(available)
        return self._recommend_healthy(available)

    def _recommend_degraded(self, available):
        needed = self._needed_positions()
        available = sorted(available, key=lambda p: p.position not in needed)
        best = available[0]
        reasons = ["NO PROJECTIONS AVAILABLE -- ranked by roster need only, not projected points."]
        reasons.extend(self._shared_reasons(best))
        return Recommendation(player=best, reasons=reasons, degraded=True, data_source=best.data_source)

    def _recommend_healthy(self, available):
        baseline = optimize_lineup(self.my_drafted_players, self.roster_positions).total_points
        scored = [
            (p, round(optimize_lineup(self.my_drafted_players + [p], self.roster_positions).total_points - baseline, 2))
            for p in available
        ]
        # Marginal value is used as a GATE ("does this player have any open
        # roster slot to fill right now"), not as the ranking metric itself.
        # Ranking directly by raw marginal points collapses to "always
        # draft whichever position scores the most per game" -- QB,
        # structurally, since it has no flex outlet and outscores every
        # other position -- which is the exact same kind of positionally-
        # blind runaway the original all-VORP bug had, just aimed at a
        # different position (confirmed empirically: an earlier version of
        # this ranked by raw marginal value alone and recommended QB for
        # 10+ consecutive rounds). VORP already correctly weighs scarcity
        # across the whole draft, not just this one roster -- it's what
        # decides "best" among candidates that pass the open-slot gate.
        #
        # Raw VORP, though, is a static snapshot -- it doesn't know that a
        # position is about to cliff off a ledge before your next turn. A
        # real mock draft exposed this: with RB and WR slots both open, the
        # tool kept taking WR (still a deep class, VORP in the 20s-30s for
        # many more rounds) over the last two non-cliff RBs (VORP ~9-11,
        # with every RB after them cratering to roughly -26 VORP) -- correct
        # by raw-VORP-right-now, but exactly backwards for draft strategy,
        # which says grab the scarce thing before the cliff and let the
        # deep position wait. self._cliff_adjusted_vorp adds a bonus sized
        # to how much value is at risk of vanishing at this player's own
        # position before your next turn -- so a smoothly-declining, deep
        # position (QB, WR here) barely moves and earns almost no bonus,
        # while a real cliff (RB here) earns a big one, without needing to
        # rely on vorp.build_tiers' gap clustering (which, applied to a
        # smooth decline, splits it into many 1-2-player tiers and made
        # ordinary decline look like a cliff during testing).
        by_position = defaultdict(list)
        for p in available:
            by_position[p.position].append(p)
        picks_until = self._picks_until_my_turn()
        floor_by_position = {
            position: self._position_floor_vorp(players, picks_until) for position, players in by_position.items()
        }

        def cliff_adjusted_vorp(player):
            return self._cliff_adjusted_vorp(player, floor_by_position[player.position])

        scored.sort(key=lambda pair: (pair[1] > 0, cliff_adjusted_vorp(pair[0])), reverse=True)

        top_pick, top_marginal = scored[0]
        best, best_marginal = top_pick, top_marginal
        reasons = []

        # Consensus dampening: don't fully trust our own board when it's a
        # big outlier vs. the field (not in FantasyPros' top 10) AND a
        # comparable, consensus-backed alternative exists AT THE SAME
        # POSITION and VORP tier. This is a binary check, not a graduated
        # one, because FantasyPros' free tier only exposes a top-10 list
        # per position -- not full rankings -- so there's no magnitude to
        # grade against. The alt search is restricted to the same position
        # (not just "same tier") and requires REAL positive consensus data
        # (_has_real_consensus_backing, not the lenient _consensus_backed)
        # -- without both restrictions this once suggested swapping a WR
        # pick for a defense, because global VORP tiers span positions and
        # DEF has no FantasyPros data at all (which the lenient check
        # treated as "nothing wrong here" instead of "no evidence either
        # way").
        if not self._consensus_backed(top_pick) and top_pick.tier is not None:
            alt = next(
                (
                    pair for pair in scored
                    if pair[0].position == top_pick.position
                    and pair[0].tier == top_pick.tier
                    and self._has_real_consensus_backing(pair[0])
                ),
                None,
            )
            if alt and alt[0].player_id != top_pick.player_id:
                best, best_marginal = alt
                reasons.append(
                    f"Preferred over {top_pick.name}: {top_pick.name} isn't in FantasyPros' consensus "
                    f"top 10 at {top_pick.position} (our own board is more of an outlier here than "
                    f"usual), while {best.name} is close in value (same tier) and consensus-backed."
                )
            else:
                reasons.append(
                    f"Note: {top_pick.name} isn't in FantasyPros' consensus top 10 at {top_pick.position} "
                    f"-- no comparable consensus-backed alternative in this tier, but this pick leans more "
                    f"on our own single-source projection than usual."
                )

        if best_marginal > 0:
            reasons.insert(
                0,
                f"Best available by VORP ({best.vorp:+.1f}) among players with open roster capacity -- "
                f"would add {best_marginal:.1f} pts to your lineup right now.",
            )
        else:
            reasons.insert(
                0,
                f"Wouldn't crack your starting lineup anywhere (best remaining talent by VORP, "
                f"{best.vorp:+.1f}) -- bench/depth pick.",
            )

        if best.tier is not None:
            reasons.append(f"Tier {best.tier} at {best.position}.")

        picks_until = self._picks_until_my_turn()
        if picks_until is not None and picks_until > 0:
            same_tier_left = sum(1 for p in available if p.position == best.position and p.tier == best.tier)
            if same_tier_left <= picks_until:
                reasons.append(
                    f"Scarcity: only {same_tier_left} Tier {best.tier} {best.position}(s) left on the "
                    f"board, and {picks_until} pick(s) happen before your next turn -- may not be there "
                    f"if you wait."
                )

        reasons.extend(self._shared_reasons(best))
        return Recommendation(player=best, reasons=reasons, degraded=False, data_source=best.data_source)

    def _shared_reasons(self, best):
        reasons = []
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

        return reasons

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
