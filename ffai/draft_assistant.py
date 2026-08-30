import logging
from collections import Counter, defaultdict

from ffai.lineup import optimize_lineup
from ffai.models import Recommendation
from ffai.repository import fetch_draft_picks
from ffai.sleeper_client import SleeperAPIError
from ffai.vorp import DEDICATED_POSITIONS, FLEX_ELIGIBILITY

logger = logging.getLogger(__name__)

POSITIONAL_RUN_WINDOW = 5
POSITIONAL_RUN_THRESHOLD = 3

# Pseudo-picks of _slot_share_prior mixed into the observed positional
# draft rates, so early picks aren't ruled by a 3-pick sample.
POSITION_RATE_PRIOR_WEIGHT = 10

# Positions where a backup has essentially no draft value because the
# waiver wire refills them week to week -- once the starting slots are
# covered, another one is a wasted pick. Without this the tool stacked
# four defenses in the late rounds of a real mock draft: with every
# starting slot full, marginal lineup value is 0 for everyone, so ranking
# fell through to raw VORP, and DEF/K screen deceptively well there
# (their replacement level is computed off a very thin pool).
STREAMABLE_POSITIONS = {"DEF", "K"}

# Round before which K/DEF are not worth listing as alternatives -- nobody
# drafts them earlier, so offering them is noise that teaches the reader to
# ignore the alternatives block entirely.
STREAMABLE_EARLIEST_ROUND = 8

# A projection gap this small is noise, not a decision. Expressed per game
# so the band scales with the season and reads in a unit that means
# something: 0.75 pts/game is under one PPR reception a week, which is well
# inside the error bars of any projection source. Below this, the tool says
# the candidates are effectively tied instead of announcing a winner --
# a real mock had it recommend Chase Brown (255.2) with Derrick Henry
# (246.9) and Saquon Barkley (246.7) still on the board, and reported that
# 8-point edge in the same confident voice it uses for a 90-point one.
# The ordering itself was right (both our projections and FantasyPros'
# consensus stat lines put Brown marginally ahead), but presenting a coin
# flip as a verdict is what made a correct pick feel broken.
NOISE_POINTS_PER_GAME = 0.75
FANTASY_SEASON_GAMES = 17
NOISE_BAND_POINTS = NOISE_POINTS_PER_GAME * FANTASY_SEASON_GAMES

# How many tied alternatives to name before summarising the rest.
MAX_NAMED_TIED_ALTERNATIVES = 3

# How far past a player's human ADP we have to be before passing on him is
# worth calling out (_market_divergence_note). Roughly a round in a 10-team
# league: inside that, board-vs-market noise is constant and flagging it
# every pick would train the reader to skip the line.
ADP_DIVERGENCE_PICKS = 10

# Bounds on how far this table's observed pace may bend the ADP forecast
# (_table_pace). The clamp is the whole point: unbounded live adaptation is
# exactly what the old positional-rate model did, and it read RB at 0.517 of
# all remaining picks off a 24-pick sample. Sample gates stop the opening
# rounds -- the least representative stretch of any draft -- from moving it.
MIN_TABLE_PACE = 0.7
MAX_TABLE_PACE = 1.6
MIN_PACE_SAMPLE = 20
MIN_PACE_SAMPLE_AT_POSITION = 5

# Roughly the share of a season a starter at each position misses, from
# typical NFL games-missed rates (RB ~3.5 games of 17, WR/TE ~2.5, QB ~2,
# K ~0.5). DEF is 0: a team defense is a unit, it is never "out".
#
# These drive _roster_value, which is what finally gives the late rounds
# something to optimise. Scoring a roster purely on its healthy starting
# lineup makes every bench pick worth EXACTLY 0, so from about round 8 on
# the objective went flat (measured: 1805 vs 1807 across every position)
# and the tool spent real picks on a kicker or a 4th TE to chase a
# rounding error. Weighting each starter by the chance they miss time
# makes the backup behind them worth something, and correctly worth
# LESS the deeper you already are -- a 2nd TE covers the TE slot, so a
# 3rd and 4th add nothing, while an RB3 behind two fragile starters is
# genuinely valuable.
#
# Approximations, deliberately: the point is to stop treating bench value
# as zero, not to forecast injuries. They also encode why nobody drafts a
# backup kicker or a second defense.
INJURY_MISS_RATE = {"RB": 0.20, "WR": 0.15, "TE": 0.15, "QB": 0.12, "K": 0.03, "DEF": 0.0}


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
        adp_ranks=None,
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

        # player_id -> the pick number human tables take him at (ffai/adp.py).
        # This is what the rest-of-draft forecast reads; see
        # _expected_available for why a per-player ADP replaced the
        # positional-rate model that used to do this job.
        self.adp_ranks = dict(adp_ranks or {})
        if self.adp_ranks:
            # Players outside the ADP sample are, by construction, ones human
            # tables don't reach in the rounds it covers. Rank them after
            # everyone who has a real ADP, in board order, so the forecast
            # still orders them sensibly rather than treating them as
            # available forever (which would make every position look deep).
            horizon = max(self.adp_ranks.values())
            for offset, player in enumerate(board, start=1):
                self.adp_ranks.setdefault(player.player_id, horizon + offset)

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

    def _slot_share_prior(self):
        """Each position's share of the league's STARTING slots, with
        flex-type slots split evenly across the positions they accept.
        Used only to seed _position_draft_rates before a draft has enough
        picks to speak for itself."""
        shares = Counter()
        for slot in self.roster_positions:
            if slot in DEDICATED_POSITIONS:
                shares[slot] += 1.0
            elif slot in FLEX_ELIGIBILITY:
                eligible = FLEX_ELIGIBILITY[slot]
                for position in eligible:
                    shares[position] += 1.0 / len(eligible)
        total = sum(shares.values())
        return {position: share / total for position, share in shares.items()} if total else {}

    def _position_draft_rates(self):
        """What fraction of picks each position is actually absorbing in
        THIS draft, smoothed toward _slot_share_prior so the first few
        picks aren't ruled by a tiny sample.

        This is the signal the tool was missing entirely, and it's why a
        real mock draft ended with two replacement-level RBs starting: RBs
        came off the board roughly three times faster than the roster's
        starting-slot mix implies (48 of the first 146 picks), so by the
        4th round the usable RB supply was nearly gone while the WR/TE
        classes this board liked were still deep. Nothing in a static VORP
        number can see that happening -- it has to be measured live."""
        counts = Counter()
        for pick in self.picks:
            player = self._by_player_id.get(pick.get("player_id"))
            if player:
                counts[player.position] += 1

        prior = self._slot_share_prior()
        total = sum(counts.values()) + POSITION_RATE_PRIOR_WEIGHT
        return {
            position: (counts[position] + POSITION_RATE_PRIOR_WEIGHT * prior.get(position, 0.0)) / total
            for position in set(prior) | set(counts)
        }

    def _expected_taken_by_next_turn(self, position, picks_until, rates):
        if picks_until is None:
            return 0
        return int(round(picks_until * rates.get(position, 0.0)))

    def _current_pick_no(self):
        """The pick number on the clock right now (1-based)."""
        return len(self.picks) + 1

    def _my_next_pick_no(self, picks_until):
        return self._current_pick_no() + (picks_until or 0)

    def _expected_available(self, position, at_pick, pools, rates, skip=0):
        """Best player at `position` still expected to be on the board at
        absolute pick number `at_pick` -- `skip` of them past the top, for
        when a plan has already spent earlier picks at this position.

        Forecast from per-player human ADP, NOT from an observed positional
        draft rate. The rate model this replaces was the single worst number
        in the recommender, for two reasons that only show up together:

        1. It measured the rate from picks SO FAR and extrapolated it over
           the whole remaining draft. But positional mix is nowhere near
           stationary -- ADP for this league's shape has RB going 12, 5, 7,
           5, 5, 5, 2 per 20 picks while K/DEF go 0, 0, 0, 0, 3, 3, 6. The
           first two rounds of any draft are RB/WR-heavy by construction, so
           the early sample is maximally unrepresentative of the rest.
        2. POSITION_RATE_PRIOR_WEIGHT (10) is swamped by pick 24, and the
           prior was the accurate half: in a real mock this computed
           (15 + 10*0.259)/(24+10) = 0.517 for RB -- i.e. that half of every
           remaining pick in the draft would be a running back, against a
           true rate of 0.293 (41 RBs in 140 picks). Extrapolated 111 picks
           ahead that assumed 57 more RBs would go, more than go in an
           entire real draft, so the rollout believed the best RB left at
           the last turn was ~RB#47 (James Conner, 57.2 pts).

        That fake cliff then fed _roster_value's injury penalty, which
        charges a roster whose RB2 has no cover -- so hoarding RBs scored as
        insurance against a shortage that was never going to happen. In the
        mock it took Breece Hall (211.0) over Nico Collins (262.0) with both
        WR slots still empty. Sweeping the rate showed the recommendation
        flipping at ~0.47: every value from the true 0.29 up to 0.45 picks
        Collins, and only the inflated estimate picks Hall.

        ADP fixes the estimate at its source rather than patching the
        extrapolation. It is per-player, already the shape of a real draft,
        and monotone in `at_pick`, which also removes the step-function
        instability that made the same comparison read 98 pts one pick and
        28 the next.

        The rate model is kept as the fallback for when ADP is unavailable
        (it's a scraped third-party source, so R2/R3 apply): a degraded
        forecast still beats no forecast, and its failure mode is now
        documented rather than silent."""
        players = pools.get(position, [])
        if self.adp_ranks:
            gone = round(self._adp_gone_by(position, players, at_pick) * self._table_pace(position))
            remaining = players[gone:]
        else:
            gone = self._expected_taken_by_next_turn(
                position, at_pick - self._current_pick_no(), rates
            )
            remaining = players[gone:]
        return remaining[skip] if skip < len(remaining) else None

    def _adp_gone_by(self, position, players, at_pick):
        """How many of `players` human ADP says are off the board by `at_pick`."""
        return sum(1 for p in players if self.adp_ranks[p.player_id] < at_pick)

    def _table_pace(self, position):
        """How much faster (or slower) THIS table is clearing `position` than
        human ADP says it should be, as a bounded multiplier.

        Pure ADP is the right prior but the wrong final answer, because a
        given table is not the average of 8,000 drafts. Replaying a real
        Sleeper mock whose bots cleared RBs far faster than humans do, the
        unadjusted ADP forecast under-read that scarcity and ended up
        starting a 170-point RB2 where the old model's 211-point one was
        better -- the one case where the discredited rate model still won.

        So: adapt, but on a leash. The multiplier is anchored to ADP's
        absolute counts rather than being a free-running rate, and clamped,
        which is precisely what the old model lacked -- it could and did run
        to 0.517/pick for RB off a 24-pick sample. At the ceiling here the
        implied RB rate is ~0.47, still below the value that produced the
        four-RB draft, and it cannot compound beyond that no matter how
        lopsided the opening is. MIN_PACE_SAMPLE keeps a handful of early
        picks from moving it at all, which is where the old estimator did
        its worst damage."""
        if len(self.picks) < MIN_PACE_SAMPLE:
            return 1.0

        here = self._current_pick_no()
        expected = sum(
            1 for pid, rank in self.adp_ranks.items()
            if rank < here and pid in self._by_player_id
            and self._by_player_id[pid].position == position
        )
        if expected < MIN_PACE_SAMPLE_AT_POSITION:
            return 1.0

        # Counted off self.picks, the same record every other forecast method
        # reads, rather than drafted_player_ids -- the two are kept in step by
        # poll_once, but only one of them is the ordered source of truth.
        actual = sum(
            1 for pick in self.picks
            if pick.get("player_id") in self._by_player_id
            and self._by_player_id[pick["player_id"]].position == position
        )
        return max(MIN_TABLE_PACE, min(MAX_TABLE_PACE, actual / expected))

    def _expected_gone_by(self, position, pools, at_pick, rates):
        """How many at `position` are expected off the board by `at_pick` --
        the count behind the "cost of waiting" reason.

        Applies _table_pace for the same reason _expected_available does: the
        number the tool PRINTS has to be the number it actually reasoned
        from, or the explanation is describing a different forecast than the
        one that made the pick."""
        players = pools.get(position, [])
        if self.adp_ranks:
            return min(
                len(players),
                round(self._adp_gone_by(position, players, at_pick) * self._table_pace(position)),
            )
        return min(
            len(players),
            self._expected_taken_by_next_turn(
                position, at_pick - self._current_pick_no(), rates
            ),
        )

    def _wait_cost(self, candidate, pools, rates, my_next, gap):
        """How many projected points you lose at `candidate`'s position by
        waiting one turn: the gap between the best there now and the best
        still expected to be there at your next turn.

        This is the tiebreak when two positions project to the SAME final
        roster, which happens constantly once your starting slots are
        nearly full -- the plan can see that pick order doesn't change
        what you end up with, so every option scores identically.

        The tie used to fall through to VORP, which is exactly backwards.
        DEF and K have inflated VORP (their replacement level is computed
        off a very thin pool, the same distortion noted in
        STREAMABLE_POSITIONS), so when everything tied they WON, and a real
        mock spent pick 89 on a defense while eight of nine rivals waited
        until round 14+. Backup QBs win the same way for the same reason.

        Wait-cost inverts that correctly and without any position
        hardcoding: nobody drafts kickers, defenses or backup QBs early, so
        their expected loss from waiting is ~0 and they sink to last --
        which is precisely why you can afford to wait on them. Positions
        that are actually evaporating keep a positive cost and rise."""
        if not gap:
            return 0.0

        fallback = self._expected_available(candidate.position, my_next + gap, pools, rates)
        if fallback is None:
            return round(candidate.points, 2)  # position may be picked clean
        return round(max(0.0, candidate.points - fallback.points), 2)

    def _my_future_pick_offsets(self, picks_until):
        """How many picks after this draft_slot's next turn each of its
        LATER turns falls, e.g. [20, 27, 40, ...] in a snake. Empty when
        the draft geometry isn't known (no total_rosters) or this is the
        last turn -- callers treat that as "nothing left to plan"."""
        if not self.total_rosters or picks_until is None:
            return []

        rounds = len(self.roster_positions)
        last_pick = rounds * self.total_rosters
        my_next = len(self.picks) + 1 + picks_until
        my_slot = int(self.my_draft_slot)

        offsets = []
        for pick_no in range(my_next + 1, last_pick + 1):
            round_no = (pick_no - 1) // self.total_rosters + 1
            pos_in_round = (pick_no - 1) % self.total_rosters + 1
            slot = pos_in_round if round_no % 2 == 1 else self.total_rosters - pos_in_round + 1
            if slot == my_slot:
                offsets.append(pick_no - my_next)
        return offsets

    def _roster_value(self, roster):
        """Expected starting-lineup points over a season, allowing for the
        fact that starters miss games.

        Healthy-lineup points plus, for each starter, the share of the
        season they're expected to miss times what the lineup drops to
        without them. A roster with real cover loses little; a roster whose
        RB2 is the last man on the bench loses a lot. That penalty is the
        only thing in the model that makes a bench pick worth more than
        zero, which is what the late rounds needed (see INJURY_MISS_RATE).

        Self-limiting for the same reason _depth_value is: once a position
        has one competent backup, the next one never enters the lineup in
        either the healthy or the injured case, so it changes nothing.

        KNOWN OVERSTATEMENT, measured and deliberately left in place: the
        depleted lineup gets no waiver replacement, so an injured starter is
        scored as if the slot were forfeited. Only 140 of 600+ draftable
        players go in a 10-team league, so the real cost is the gap to a
        freely available player. This makes backups look better than they
        are -- on a half-human mock it drafted a QB2 that added 0.0 points to
        its starting lineup and 36.4 to its score. Crediting replacement
        level instead was tried and measured +1.8 / +2.9 healthy points on
        two opponent models but -17.3 on a third, i.e. not a clear win, and
        it was reverted rather than shipped on ambiguous evidence. Worth
        revisiting with time to tune the floor (replacement level is
        probably too generous a stand-in for what waivers actually offer
        mid-season)."""
        lineup = optimize_lineup(roster, self.roster_positions)
        healthy = lineup.total_points

        penalty = 0.0
        for slot in lineup.slots:
            starter = slot.player
            if starter is None:
                continue
            miss_rate = INJURY_MISS_RATE.get(starter.position, 0.0)
            if not miss_rate:
                continue
            without = [p for p in roster if p.player_id != starter.player_id]
            depleted = optimize_lineup(without, self.roster_positions).total_points
            penalty += miss_rate * (healthy - depleted)

        return round(healthy - penalty, 2)

    def _plan_value(self, first_choice, pools, rates, my_next, future_offsets):
        """Projected total starting-lineup points of the roster you end the
        draft with if you take `first_choice` now and then keep taking
        whatever helps most at each of your remaining turns.

        This is the whole point of the recommender, and getting here took
        two wrong turns worth recording. Ranking by static value (VORP)
        ignored that positions deplete at different speeds. Ranking by the
        one-turn cost of waiting fixed nothing, because it's a greedy trap:
        skipping RB costs almost nothing at ANY single turn (the next RB is
        only a few points worse), so the tool deferred RB every round in
        turn and ended a real mock draft starting two replacement-level
        RBs. Only looking all the way to the end of the draft exposes that
        -- the cost isn't in any one deferral, it's in the compounding.

        The rollout is greedy per future turn, and the estimate of who'll
        still be there comes from _expected_available, so this is an
        approximation of the future, not a forecast of it. It doesn't need
        to be exact: it only has to rank a handful of positions correctly
        against each other right now. It does need to be roughly UNBIASED
        across positions, which is where the old rate model failed -- see
        _expected_available."""
        roster = list(self.my_drafted_players) + [first_choice]
        taken_by_me = Counter({first_choice.position: 1})

        for elapsed in future_offsets:
            current = self._roster_value(roster)
            best_candidate = None
            best_gain = None
            for position in pools:
                candidate = self._expected_available(
                    position, my_next + elapsed, pools, rates, skip=taken_by_me[position]
                )
                if candidate is None:
                    continue
                gain = self._roster_value(roster + [candidate]) - current
                if best_gain is None or gain > best_gain:
                    best_gain, best_candidate = gain, candidate
            if best_candidate is None:
                break
            roster.append(best_candidate)
            taken_by_me[best_candidate.position] += 1

        return self._roster_value(roster)

    def _depth_value(self, candidate, lineup, losses=1):
        """What the candidate would add if your `losses` weakest current
        starters at their position went down -- their value as cover there.

        This is what separates a useful depth pick from a dead one once
        every starting slot is full, and it self-limits without any
        per-position roster caps.

        `losses` exists because the single-loss version quietly failed the
        claim its own docstring used to make. It does NOT return 0 for a
        redundant backup: it returns the UPGRADE over the backup you
        already have, which is positive whenever the next man is even
        marginally better. In a real mock that read +2.10 for a third tight
        end (Kelce 171.4 over Kittle 169.3) behind a starter who never
        misses a lineup -- enough to clear a `> 0` gate, so TE stayed
        eligible for bench picks forever and the draft ended with four of
        them. Meanwhile RB and WR depth scored exactly 0.00 and were
        dropped from consideration entirely, at the two positions with two
        starting slots each and the highest injury rates on the board.

        Both numbers were right; the question was wrong. Under one loss a
        roster with a single backup anywhere IS covered, so nothing
        rates -- and the tie fell to whichever position happened to offer a
        rounding-error upgrade. Asking about two simultaneous losses is
        what makes redundancy visible: a fourth TE still cannot cover more
        than the one TE slot (there is only one starter to lose, so this
        degenerates to the single-loss answer and stays ~0), while a third
        RB covers a genuine hole, because losing two of RB1/RB2/FLEX leaves
        a slot no one on the roster can fill. Seasons have byes and
        multiple injuries; one-at-a-time was the unrealistic assumption."""
        starters_at_position = [
            slot.player for slot in lineup.slots
            if slot.player is not None and slot.player.position == candidate.position
        ]
        if not starters_at_position:
            return 0.0  # nobody starting there yet -- marginal value already covers that case

        # min() guards the degenerate case above: asking about two losses at
        # a position that only starts one player is just the one-loss
        # question, which is exactly why redundant TEs stop qualifying.
        weakest = sorted(starters_at_position, key=lambda p: p.points)[:max(1, losses)]
        weakest_ids = {p.player_id for p in weakest}
        depleted = [p for p in self.my_drafted_players if p.player_id not in weakest_ids]
        before = optimize_lineup(depleted, self.roster_positions).total_points
        after = optimize_lineup(depleted + [candidate], self.roster_positions).total_points
        return round(after - before, 2)

    def _draftable_candidates(self, available):
        """Drops backups at STREAMABLE_POSITIONS once their starting slots
        are covered -- a 2nd DEF/K is a wasted pick when the waiver wire
        refills those weekly. Falls back to the unfiltered list if that
        would leave nothing (deep into a draft where only DEF/K remain),
        so this can never strand the recommender with no candidate."""
        required = Counter(pos for pos in self.roster_positions if pos in STREAMABLE_POSITIONS)
        mine = Counter(p.position for p in self.my_drafted_players)
        covered = {pos for pos, count in required.items() if mine[pos] >= count}
        if not covered:
            return available

        filtered = [p for p in available if p.position not in covered]
        return filtered or available

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
        # Every candidate is scored by COST OF WAITING: how much better it
        # is to take this player now than to take the best player at his
        # position that this draft_slot can still realistically expect at
        # its next turn. Both halves are measured in marginal starting-
        # lineup points (lineup.optimize_lineup), so they're directly
        # comparable across positions and already account for which of
        # your slots are open, flex included.
        #
        # This replaces ranking by VORP, which is a season-long valuation,
        # not a draft-decision one. Two real mock drafts showed why that
        # distinction matters: VORP happily kept spending early picks on a
        # deep WR/TE class it rated highly while RBs -- a position with two
        # mandatory starting slots -- were coming off the board three
        # times faster, leaving replacement-level RBs starting. Cost of
        # waiting sees that directly: if skipping a position costs you
        # almost nothing because a near-equal player will still be there
        # (deep WR class), it ranks low; if it costs you 40 points because
        # the position is evaporating (RB), it ranks high.
        #
        # Also deliberately not raw marginal value, which collapses to
        # "always draft the position that scores the most per game" -- QB,
        # structurally, since it has no flex outlet (confirmed empirically:
        # an earlier version ranked that way and recommended QB 10+ rounds
        # straight). Subtracting the wait-value cancels that scale bias,
        # because it's a within-position difference.
        baseline = optimize_lineup(self.my_drafted_players, self.roster_positions).total_points

        def marginal(player):
            lineup = optimize_lineup(self.my_drafted_players + [player], self.roster_positions)
            return round(lineup.total_points - baseline, 2)

        candidates = self._draftable_candidates(available)
        pools = defaultdict(list)
        for p in candidates:
            pools[p.position].append(p)

        picks_until = self._picks_until_my_turn()
        my_next = self._my_next_pick_no(picks_until)
        rates = self._position_draft_rates()
        future_offsets = self._my_future_pick_offsets(picks_until)

        # How many picks happen between the pick being made RIGHT NOW and
        # my next one -- the gap every scarcity question is really asking
        # about ("how many RBs go before I'm back?").
        #
        # NOT _picks_until_my_turn(), which measures the distance until I'm
        # ON the clock and is therefore 0 during every live recommendation,
        # silently zeroing _expected_taken_by_next_turn and with it the
        # cost-of-waiting reason and the wait-cost tiebreak. It only ever
        # read non-zero before the first pick of the draft, which is why a
        # real mock showed "Cost of waiting" in its pre-draft recommendation
        # and never again. future_offsets[0] is already this gap.
        gap_to_next_turn = future_offsets[0] if future_offsets else picks_until

        # Within a position you'd always take the better projection, so the
        # only real decision at any pick is WHICH POSITION -- a handful of
        # options, not hundreds of players. That's what makes it affordable
        # to plan the entire rest of the draft for each one below.
        #
        # Positions that can neither improve your lineup now nor cover a
        # starter going down are dropped outright: they're pure waste, and
        # leaving them in is how a real mock draft ended up with four
        # bench TEs (see _depth_value).
        current_lineup = optimize_lineup(self.my_drafted_players, self.roster_positions)

        def worth_considering(player):
            if marginal(player) > 0:
                return True
            # Cover has to be worth a real pick, not a rounding error. A bare
            # "> 0" let a +2.10 upgrade at an already-double-covered position
            # keep qualifying round after round; NOISE_BAND_POINTS is the same
            # threshold used everywhere else here to decide a season-long gap
            # is too small to act on.
            if self._depth_value(player, current_lineup) > NOISE_BAND_POINTS:
                return True
            # And ask the question one-at-a-time cover cannot answer: if two
            # starters at this position went down, is there anybody left? This
            # is what readmits RB/WR depth -- both score exactly 0.00 under a
            # single loss once any backup exists -- while leaving a redundant
            # 3rd/4th TE at ~0, since a position that starts one player has
            # only one starter to lose.
            return self._depth_value(player, current_lineup, losses=2) > NOISE_BAND_POINTS

        options = [players[0] for players in pools.values() if worth_considering(players[0])]
        if not options:
            options = [players[0] for players in pools.values()]

        if not future_offsets:
            # Nothing left to plan against (draft geometry unknown, or this
            # is the last pick): take the best real contribution available --
            # an open starting slot if there is one, otherwise genuine cover.
            #
            # This used to rank by raw VORP once no slot was open, which
            # hands the pick to whichever position's replacement level is
            # computed off the thinnest pool. That is how a real mock spent
            # its FINAL pick on a fourth tight end at VORP -1.1, behind a
            # starter who never leaves the lineup, with a bye-week collision
            # the tool printed in its own reasons.
            # Cover is contingent, an open starting slot is not, so they
            # cannot be compared at face value: a backup worth 20 pts only
            # in the weeks his starter is out is not worth more than a
            # kicker who scores 9 every week. Discount each cover case by
            # how much of a season it actually applies to -- one starter's
            # missed share, and for two at once the product, since two
            # absences have to coincide.
            def contribution(player):
                miss_rate = INJURY_MISS_RATE.get(player.position, 0.0)
                return max(
                    marginal(player),
                    miss_rate * self._depth_value(player, current_lineup),
                    miss_rate**2 * self._depth_value(player, current_lineup, losses=2),
                )

            scored = [(p, contribution(p)) for p in options]
            scored.sort(key=lambda pair: (pair[1], pair[0].vorp), reverse=True)
        else:
            scored = [(p, self._plan_value(p, pools, rates, my_next, future_offsets)) for p in options]
            # Near-ties are the common case late, when you'll simply end up
            # with both positions and pick order barely changes the final
            # roster. Everything within NOISE_BAND_POINTS of the leader is
            # treated as tied and ranked by URGENCY instead: take the
            # position that won't still be there next turn, defer the one
            # that will.
            #
            # The band matters as much as the tiebreak. Exact ties alone
            # left defenses going in round 9 on a TWO POINT plan-value edge
            # -- real, but a season-long projection gap that small at the
            # position with the least reliable projections in fantasy is
            # noise, and chasing it cost a pick that had somewhere better
            # to be. VORP is only the last resort, because on its own it
            # hands every tie to DEF/K (see _wait_cost).
            best_plan = max(value for _player, value in scored)
            scored.sort(
                key=lambda pair: (
                    pair[1] >= best_plan - NOISE_BAND_POINTS,
                    self._wait_cost(pair[0], pools, rates, my_next, gap_to_next_turn),
                    pair[1],
                    pair[0].vorp,
                ),
                reverse=True,
            )

        top_pick = scored[0][0]
        best = top_pick
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
        #
        # "Comparable" also has to mean comparable in POINTS, not just
        # tier. Tiers are gap-clustered and can span 30+ points at thin
        # positions, and without a magnitude guard this swap fired on a
        # 27.6-point downgrade (Kincaid 163.6 -> Goedert 136.0, same tier,
        # Goedert consensus-backed). It then cost a second pick: with the
        # TE slot filled by the weaker player, the better one was still the
        # biggest available upgrade a round later, so the tool drafted him
        # too and benched the first. One unguarded swap, two wasted picks.
        # NOISE_BAND_POINTS is the same "this gap is noise" threshold the
        # coin-flip note uses -- if the alternative isn't inside it, our
        # board isn't an outlier, it just disagrees.
        if not self._consensus_backed(top_pick) and top_pick.tier is not None:
            alt = next(
                (
                    p for p in pools[top_pick.position]
                    if p.tier == top_pick.tier
                    and self._has_real_consensus_backing(p)
                    and abs(top_pick.points - p.points) <= NOISE_BAND_POINTS
                ),
                None,
            )
            if alt and alt.player_id != top_pick.player_id:
                best = alt
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

        best_marginal = marginal(best)
        if best_marginal > 0:
            reasons.insert(
                0,
                f"Fills an open starting slot -- adds {best_marginal:.1f} pts to your lineup right now "
                f"(VORP {best.vorp:+.1f}).",
            )
        else:
            reasons.insert(
                0,
                f"Wouldn't crack your starting lineup anywhere (best remaining talent by VORP, "
                f"{best.vorp:+.1f}) -- bench/depth pick.",
            )

        # Second, right under the headline: if this is a coin flip the
        # reader needs to know before the confident-sounding reasons below,
        # not after them.
        tie_note = self._tie_note(best, pools)
        if tie_note:
            reasons.insert(1, tie_note)

        if best.tier is not None:
            reasons.append(f"Tier {best.tier} at {best.position}.")

        if future_offsets and len(scored) > 1:
            runner_up, runner_up_value = scored[1]
            edge = scored[0][1] - runner_up_value
            if edge > NOISE_BAND_POINTS:
                reasons.append(
                    f"Playing out the rest of your draft from here, taking {best.position} now projects to "
                    f"a {edge:.0f} pt better final lineup than going {runner_up.position} "
                    f"({runner_up.name}) -- because of what's likely to still be there at your later picks, "
                    f"not just who's best right now."
                )
            elif edge > 0:
                # Inside the noise band the ranking has ALREADY treated these
                # two as tied and broken them on urgency (see the sort above),
                # so reporting the raw gap as though it decided the pick
                # contradicts the logic that actually made it. A real mock
                # printed "a 5 pt better final lineup" for a 3.0-point gap at
                # a band of 12.75, in the same voice it uses for 90-point
                # edges -- the reader has no way to tell those apart.
                reasons.append(
                    f"{best.position} and {runner_up.position} ({runner_up.name}) project to "
                    f"within {edge:.0f} pts of the same final lineup -- too close to call, so this "
                    f"comes down to which one won't still be there at your next turn ({best.position}). "
                    f"Taking {runner_up.name} instead is defensible."
                )

        if gap_to_next_turn:
            next_turn_pick = my_next + gap_to_next_turn
            expected_gone = self._expected_gone_by(best.position, pools, next_turn_pick, rates)
            fallback = self._expected_available(best.position, next_turn_pick, pools, rates)
            if fallback is None:
                reasons.append(
                    f"Cost of waiting: on human ADP the {best.position} board may be picked clean "
                    f"before your next turn ({gap_to_next_turn} picks away)."
                )
            elif expected_gone > 0:
                drop = best.points - fallback.points
                if drop > 0:
                    reasons.append(
                        f"Cost of waiting: ~{expected_gone} more {best.position}(s) should go in the "
                        f"{gap_to_next_turn} picks before your next turn, leaving {fallback.name} "
                        f"({drop:.0f} pts worse) as the likely best available."
                    )

        reasons.extend(self._shared_reasons(best))

        divergence = self._market_divergence_note(best, available)
        if divergence:
            reasons.append(divergence)

        return Recommendation(
            player=best, reasons=reasons, degraded=False, data_source=best.data_source,
            alternatives=self._alternatives(best, pools, marginal),
        )

    def _tied_alternatives(self, best, pools):
        """Still-available players at `best`'s position whose projection is
        inside NOISE_BAND_POINTS of his, in board order.

        Deliberately same-position only. A cross-position gap is already
        reported as the plan-value edge, and that one IS a real decision
        even when it's small -- it's the difference between two different
        final rosters. This is the other case: same slot, same plan, a
        projection gap too thin to justify the word "recommended"."""
        return [
            p for p in pools[best.position]
            if p.player_id != best.player_id
            and abs(best.points - p.points) <= NOISE_BAND_POINTS
        ]

    def _tie_note(self, best, pools):
        tied = self._tied_alternatives(best, pools)
        if not tied:
            return None

        named = tied[:MAX_NAMED_TIED_ALTERNATIVES]
        names = ", ".join(p.name for p in named)
        remainder = len(tied) - len(named)
        if remainder:
            names += f" and {remainder} other{'s' if remainder > 1 else ''}"
        return (
            f"Effectively a coin flip: {names} project within "
            f"{NOISE_POINTS_PER_GAME:g} pts/game of {best.name} -- inside the margin of error, so "
            f"any of them is a defensible pick here. {best.name} is the narrow edge, not a verdict."
        )

    def _market_divergence_note(self, best, available):
        """Name the best player the market rates far above where our board
        does, when we're passing on him right here.

        A projection-driven board will sometimes disagree with human ADP by
        a whole round or more, and that disagreement is legitimate -- it's
        most of the tool's edge. What isn't legitimate is making it
        SILENTLY. In a real mock our board had Ashton Jeanty at RB12 (both
        Sleeper and FantasyPros' consensus put him outside the top 10 at the
        position) while human ADP took him 14th overall; he simply never
        appeared in a recommendation, and the reader had no way to tell an
        active call from an oversight. Say it out loud so the disagreement
        can be judged, the same way the FantasyPros consensus note already
        does for our own outliers."""
        if not self.adp_ranks:
            return None

        here = self._current_pick_no()
        reaches = [
            p for p in available
            if p.player_id != best.player_id
            and self.adp_ranks.get(p.player_id, here) <= here - ADP_DIVERGENCE_PICKS
            # Only a gap our own board considers REAL is a disagreement worth
            # reporting. Without this the note fired on players inside the
            # coin-flip band (228 vs 229 pts) -- where "we rate him lower" is
            # not a claim we're making -- and duplicated the tie note that had
            # already listed them as interchangeable.
            and best.points - p.points > NOISE_BAND_POINTS
        ]
        if not reaches:
            return None

        # The one the market is highest on, not the one our board likes most:
        # the question this answers is "who is everyone else taking here?"
        headline = min(reaches, key=lambda p: self.adp_ranks[p.player_id])
        behind = here - self.adp_ranks[headline.player_id]
        return (
            f"Passing on {headline.name} ({headline.position}), who human drafts take around pick "
            f"{self.adp_ranks[headline.player_id]:.0f} -- {behind:.0f} picks ago. Our projection has "
            f"him at {headline.points:.0f} pts (VORP {headline.vorp:+.1f}), below {best.name}'s "
            f"{best.points:.0f}. That's a real disagreement with the market, not an oversight -- "
            f"but it's our board against the room, so weigh it."
        )

    def _alternatives(self, best, pools, marginal):
        """Best available at every position that still has an EMPTY starting
        slot, so the reader can overrule the recommendation without having to
        go hunting on the Sleeper board.

        This exists because a single recommendation is a bad interface for a
        user who does not know football well. Told only "take this TE", the
        honest options are to obey or to fall back on the two heuristics a
        newcomer actually has -- highest projected number, or most familiar
        name -- and both are worse than the board. Name recognition in
        particular is a trap the projections already price in: the well-known
        player is exactly the one whose price the market has bid up.

        So each line carries the two things a name cannot tell you: what we
        project, and where the market takes him. A player still on the board
        past his ADP is one the room has passed on and we have not -- the
        "sneaky" pick, flagged explicitly rather than left for the reader to
        infer."""
        lines = []
        here = self._current_pick_no()
        # K/DEF technically have an empty starting slot from pick 1, but
        # offering them in round 2 is noise that trains the reader to skip
        # this whole block. They only become a real option once the rounds
        # nobody drafts them in are behind us.
        late = self.total_rosters and here > self.total_rosters * STREAMABLE_EARLIEST_ROUND
        for position in ("QB", "RB", "WR", "TE", "K", "DEF"):
            if position in STREAMABLE_POSITIONS and not late:
                continue
            players = pools.get(position, [])
            if not players:
                continue
            top = players[0]
            if top.player_id == best.player_id or marginal(top) <= 0:
                continue  # already the pick, or wouldn't crack the lineup

            # The value play at this position: whoever we rate closest to the
            # top man while the market rates him materially later. Only worth
            # naming when it's someone OTHER than the obvious pick.
            sneaky = None
            if self.adp_ranks:
                falling = [
                    p for p in players[:8]
                    if p.player_id != top.player_id
                    and self.adp_ranks.get(p.player_id, 0) < here
                    and top.points - p.points <= NOISE_BAND_POINTS
                ]
                if falling:
                    sneaky = max(falling, key=lambda p: here - self.adp_ranks[p.player_id])

            lines.append(f"{position:4s} {top.name} -- {top.points:.0f} pts{self._market_tag(top, here)}")
            if sneaky is not None:
                lines.append(
                    f"       value alt: {sneaky.name} -- {sneaky.points:.0f} pts"
                    f"{self._market_tag(sneaky, here)}, "
                    f"within {top.points - sneaky.points:.0f} pts of {top.name.split()[-1]}"
                )
        return lines

    def _market_tag(self, player, here):
        """Where the room takes this player, relative to where we are now."""
        rank = self.adp_ranks.get(player.player_id)
        if not rank:
            return ""
        if rank < here:
            return f", MARKET PASSED HIM (goes ~{rank:.0f}, we're at {here})"
        return f", market ~pick {rank:.0f}"

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
    if recommendation.alternatives:
        # Printed for every pick, not just close ones: the reader overruling
        # this needs the same information whether or not the tool thought the
        # call was close.
        lines.append("  IF YOU DISAGREE -- best available where you still have an empty starting slot:")
        for alt in recommendation.alternatives:
            lines.append(f"    {alt}")
    return "\n".join(lines)
