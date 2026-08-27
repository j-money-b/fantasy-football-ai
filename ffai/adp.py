"""Average draft position from real human drafts.

ADP ("average draft position") is the average pick number a player goes
at across a large sample of real drafts. It is the only cheap source of
truth for what human managers actually do, as opposed to what a
projection column says they should do -- and the two disagree sharply,
which is the whole reason this module exists.

Every draft the recommender had been validated against was played by
Sleeper autopick bots, which take the highest projected total available.
That is the degenerate strategy `backtest.py` scores at 357 points: it
drafts quarterbacks near-exclusively and clears the running back board
far earlier than a human table ever would. Tuning positional-scarcity
logic against that risks fitting the bots rather than the league.

Source is Fantasy Football Calculator rather than FantasyPros: FP serves
its ADP table from JavaScript with nothing in the HTML, and its free tier
gates projections past the top 10 per position (see
projections.fetch_consensus_top10). FFC publishes an unauthenticated JSON
endpoint, built from real human mock drafts, filterable to the exact
league shape we care about -- 10 teams, PPR -- which matters because ADP
is shape-dependent: a 12-team board and a 10-team board do not deplete
at the same rate.
"""
import logging
from datetime import datetime, timedelta, timezone

import requests

from ffai.config import ADP_HOST, ADP_MAX_AGE_HOURS
from ffai.projections import _match_fantasypros_player

logger = logging.getLogger(__name__)

ADP_CACHE_KEY_PREFIX = "adp:ppr"

# FFC's position labels differ from Sleeper's in one place.
POSITION_ALIASES = {"PK": "K"}

# A sample this thin means FFC has not accumulated enough drafts for the
# season yet (or the params were wrong), and the resulting order would be
# noise dressed up as consensus.
MIN_TOTAL_DRAFTS = 200
MIN_PLAYERS = 150


class AdpFetchError(Exception):
    """FFC unreachable, malformed, or too thin a sample to trust."""


def _sleeper_id(entry, players_raw):
    """FFC entry -> Sleeper player_id, or None if it can't be matched
    unambiguously.

    Two structural mismatches, both of which look like fuzzy-name failures
    but aren't -- worth handling explicitly rather than widening the name
    matcher, which would start producing wrong matches elsewhere:

    - Kickers are position "PK" at FFC, "K" at Sleeper.
    - Defenses are named "Seattle Defense" at FFC, while Sleeper keys them
      by bare team abbreviation ("SEA") with a full_name of "Seattle
      Seahawks". No name normalisation bridges that, so match on the team
      code, which both sides agree on.

    Together those two accounted for every single unmatched entry (47 of
    266) on the first run; with them handled the match rate is total.
    """
    position = POSITION_ALIASES.get(entry.get("position"), entry.get("position"))
    team = entry.get("team")

    if position == "DEF":
        candidate = players_raw.get(team)
        if candidate and candidate.get("position") == "DEF":
            return team
        return None

    return _match_fantasypros_player(entry.get("name", ""), team, position, players_raw)


def fetch_adp(cache, players_raw, teams, season, scoring="ppr", max_age_hours=ADP_MAX_AGE_HOURS):
    """Ordered list of Sleeper player_ids, earliest ADP first.

    Cached under the same "serve if still fresh" policy as the player
    dictionary (repository.fetch_players): ADP moves on the timescale of
    news cycles, not minutes, and re-pulling it on every backtest run
    hammers a free endpoint for no benefit. A live failure falls back to
    the stale cache; only a failure with nothing cached raises.

    Returns (player_ids, stale, meta). `meta` carries FFC's own sample
    description -- how many drafts, over what dates -- because an ADP
    order is only as good as the sample under it, and a caller reporting
    results should be able to say which sample it used.
    """
    key = f"{ADP_CACHE_KEY_PREFIX}:{scoring}:{teams}:{season}"
    cached = cache.get(key)

    if cached is not None:
        payload, fetched_at = cached
        age = datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at)
        if age < timedelta(hours=max_age_hours):
            return payload["player_ids"], False, payload["meta"]

    try:
        payload = _fetch_live(players_raw, teams, season, scoring)
    except AdpFetchError as exc:
        if cached is None:
            raise
        logger.warning("ADP fetch failed (%s); serving stale cache", exc)
        stale_payload, _fetched_at = cached
        return stale_payload["player_ids"], True, stale_payload["meta"]

    cache.set(key, payload)
    return payload["player_ids"], False, payload["meta"]


def _fetch_live(players_raw, teams, season, scoring):
    url = f"{ADP_HOST}/api/v1/adp/{scoring}"
    try:
        response = requests.get(
            url,
            params={"teams": teams, "year": season, "position": "all"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise AdpFetchError(str(exc)) from exc

    entries = data.get("players") or []
    meta = data.get("meta") or {}

    if meta.get("total_drafts", 0) < MIN_TOTAL_DRAFTS:
        raise AdpFetchError(f"only {meta.get('total_drafts')} drafts in sample")

    ordered = sorted(entries, key=lambda e: e.get("adp", 9999))
    player_ids = []
    unmatched = []
    for entry in ordered:
        player_id = _sleeper_id(entry, players_raw)
        if player_id is None:
            unmatched.append(entry.get("name"))
            continue
        if player_id not in player_ids:
            player_ids.append(player_id)

    if len(player_ids) < MIN_PLAYERS:
        raise AdpFetchError(f"only {len(player_ids)} of {len(entries)} entries matched a Sleeper player")

    if unmatched:
        logger.warning("ADP: %d entries unmatched (%s)", len(unmatched), ", ".join(unmatched[:5]))

    return {"player_ids": player_ids, "meta": meta}
