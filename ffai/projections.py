import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup

from ffai.config import ESPN_HOST, FANTASYPROS_HOST, SLEEPER_PROJECTIONS_HOST

logger = logging.getLogger(__name__)

# Sleeper is the sole source of weekly projections, so a transient blip there
# is a total outage for the brief rather than a downgrade to a second source.
SLEEPER_FETCH_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 1.0

REQUEST_TIMEOUT_SECONDS = 15
USER_AGENT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

DEFAULT_POSITIONS = ["QB", "RB", "WR", "TE", "K", "DEF"]
# FantasyPros' free Projections pages were only verified (Milestone 0) for the
# skill positions -- K/DST slugs and column layouts are unconfirmed, and
# top-10-at-position is low-value for those positions anyway.
CONSENSUS_POSITIONS = ["QB", "RB", "WR", "TE"]

MIN_PAYLOAD_SIZE = 50
MAX_PAYLOAD_SIZE = 8000
# Season-aggregate plausibility bounds (generous guard-rails, not tight limits --
# R4: catch corruption, not flag every outlier superstar season).
STAT_BOUNDS = {
    "pass_yd": 6000, "pass_td": 60, "pass_int": 30,
    "rush_att": 450, "rush_yd": 2400, "rush_td": 30,
    "rec": 160, "rec_yd": 2400, "rec_td": 22,
    "fum_lost": 15,
}
MAX_BOUND_VIOLATION_FRACTION = 0.05
MIN_COVERAGE_FRACTION = 0.9

# Sourced from https://github.com/cwendt94/espn-api/blob/master/espn_api/football/constant.py
# (PLAYER_STATS_MAP), spot-checked against a real player's decoded projection
# in Milestone 0 for internal consistency (yards-per-attempt in a plausible
# range, proportional receiving involvement). K/DEF distance- and
# points-allowed-bucket stats are deliberately NOT mapped: ESPN's bucket
# boundaries (e.g. 14-17, 18-21) don't align with Sleeper's/most leagues'
# (e.g. 14-20), and guessing a bucket mapping risks silently misscoring those
# positions -- aggregate-only fields are mapped instead.
ESPN_STAT_MAP = {
    0: "pass_att", 1: "pass_cmp", 3: "pass_yd", 4: "pass_td", 19: "pass_2pt", 20: "pass_int",
    23: "rush_att", 24: "rush_yd", 25: "rush_td", 26: "rush_2pt",
    41: "rec", 42: "rec_yd", 43: "rec_td", 44: "rec_2pt", 58: "rec_tgt",
    68: "fum", 72: "fum_lost",
    83: "fgm", 84: "fga", 85: "fgmiss", 86: "xpm", 87: "xpa", 88: "xpmiss",
    95: "int", 96: "fum_rec", 97: "blk_kick", 98: "safe", 99: "sack", 106: "ff",
}
ESPN_POSITION_ID_MAP = {0: "QB", 2: "RB", 4: "WR", 6: "TE", 16: "DEF", 17: "K"}

FANTASYPROS_COLUMN_TO_RAW_KEY = {
    "PASSING ATT": "pass_att", "PASSING CMP": "pass_cmp", "PASSING YDS": "pass_yd",
    "PASSING TDS": "pass_td", "PASSING INTS": "pass_int",
    "RUSHING ATT": "rush_att", "RUSHING YDS": "rush_yd", "RUSHING TDS": "rush_td",
    "RECEIVING REC": "rec", "RECEIVING YDS": "rec_yd", "RECEIVING TDS": "rec_td",
    "MISC FL": "fum_lost",
}


class ProjectionsFetchError(Exception):
    """Raised internally by _fetch_* helpers; always caught inside fetch()."""


@dataclass
class ProjectionsResult:
    raw_stats_by_player: dict
    source: str  # "sleeper" | "espn" | "none"
    degraded: bool
    warnings: list = field(default_factory=list)


@dataclass
class ConsensusEntry:
    name: str
    team: str
    position: str
    raw_stats: dict
    player_id: "str | None" = None


class ProjectionsAdapter:
    """R5: the one file that talks to undocumented/scraped third-party hosts.
    Everything downstream (scoring, VORP, draft assistant) only ever sees the
    normalized raw_stats_by_player dict from ProjectionsResult, never a
    Sleeper/ESPN/FantasyPros-shaped payload directly."""

    def __init__(
        self,
        sleeper_host: str = SLEEPER_PROJECTIONS_HOST,
        espn_host: str = ESPN_HOST,
        fantasypros_host: str = FANTASYPROS_HOST,
        timeout: int = REQUEST_TIMEOUT_SECONDS,
        retry_backoff_seconds: float = RETRY_BACKOFF_SECONDS,
    ):
        self.sleeper_host = sleeper_host
        self.espn_host = espn_host
        self.fantasypros_host = fantasypros_host
        self.timeout = timeout
        self.retry_backoff_seconds = retry_backoff_seconds

    def fetch(self, players_raw, season, week=None, positions=DEFAULT_POSITIONS, known_player_ids=None):
        """Sleeper -> ESPN -> degraded. `players_raw` is the full Sleeper
        player dictionary, needed for the ESPN espn_id crosswalk. `week=None`
        means season aggregate (confirmed native support on Sleeper's
        endpoint in Milestone 0 -- no week segment needed)."""
        warnings = []

        try:
            raw = self._fetch_sleeper(season, week, positions)
        except ProjectionsFetchError as exc:
            warnings.append(f"Sleeper projections fetch failed: {exc}")
        else:
            ok, val_warnings = self._validate(raw, known_player_ids)
            warnings.extend(val_warnings)
            if ok:
                return ProjectionsResult(raw, source="sleeper", degraded=False, warnings=warnings)
            warnings.append("Sleeper projections failed validation, falling back to ESPN")

        try:
            raw = self._fetch_espn(players_raw, season, week, positions)
        except ProjectionsFetchError as exc:
            warnings.append(f"ESPN projections fetch failed: {exc}")
        else:
            ok, val_warnings = self._validate(raw, known_player_ids)
            warnings.extend(val_warnings)
            if ok:
                return ProjectionsResult(raw, source="espn", degraded=False, warnings=warnings)
            warnings.append("ESPN projections failed validation")

        warnings.append("All projection sources exhausted or failed validation")
        return ProjectionsResult({}, source="none", degraded=True, warnings=warnings)

    def fetch_consensus_top10(self, players_raw, positions=CONSENSUS_POSITIONS):
        """Supplementary cross-check, NOT part of the fallback chain above --
        only the top 10 players per position are served by FantasyPros'
        anonymous free tier (confirmed in Milestone 0; the rest sits behind a
        registration gate), too thin to be a real fallback for the whole
        pool. Used by draft_assistant.py for early-round reasoning only."""
        result = {}
        for position in positions:
            try:
                entries = self._fetch_fantasypros(position)
            except ProjectionsFetchError:
                result[position] = []
                continue
            for entry in entries:
                entry.player_id = _match_fantasypros_player(entry.name, entry.team, entry.position, players_raw)
            result[position] = [e for e in entries if e.player_id is not None]
        return result

    def _get_with_retry(self, url, **kwargs):
        """GET, retrying transient failures only.

        Sleeper is the ONLY source of WEEKLY projections -- _fetch_espn
        refuses week != None outright as an unverified shape, and
        FantasyPros' anonymous tier stops at 10 players per position. So
        unlike the season-long path, a single timed-out request here leaves
        the brief with no projections at all rather than degrading to a
        second source. Replaying the 2025 season hit exactly that: one week
        of fourteen died on a 15-second read timeout and produced nothing,
        and it succeeded on a plain retry seconds later.

        Retries timeouts, connection errors and 5xx (the server's problem,
        likely different next time). A 4xx is a permanent answer -- wrong
        URL, wrong params -- and retrying it just delays an honest failure."""
        last_exc = None
        for attempt in range(SLEEPER_FETCH_ATTEMPTS):
            try:
                response = requests.get(url, timeout=self.timeout, **kwargs)
                if response.status_code >= 500:
                    raise ProjectionsFetchError(f"server error {response.status_code}")
                response.raise_for_status()
                return response
            except (requests.Timeout, requests.ConnectionError, ProjectionsFetchError) as exc:
                last_exc = exc
            except requests.RequestException as exc:
                raise ProjectionsFetchError(str(exc)) from exc  # 4xx: permanent, don't retry

            if attempt < SLEEPER_FETCH_ATTEMPTS - 1:
                logger.warning("projections fetch attempt %d failed (%s); retrying", attempt + 1, last_exc)
                time.sleep(self.retry_backoff_seconds * (attempt + 1))

        raise ProjectionsFetchError(str(last_exc))

    def _fetch_sleeper(self, season, week, positions):
        if week is None:
            url = f"{self.sleeper_host}/projections/nfl/{season}"
        else:
            url = f"{self.sleeper_host}/projections/nfl/{season}/{week}"
        params = {"season_type": "regular", "position[]": positions}
        response = self._get_with_retry(url, params=params)

        data = response.json()
        if not isinstance(data, list):
            raise ProjectionsFetchError(f"unexpected response shape: {type(data)}")

        return {row["player_id"]: row.get("stats", {}) for row in data if row.get("player_id")}

    def _fetch_espn(self, players_raw, season, week, positions):
        if week is not None:
            # ESPN's weekly-projection payload shape is undocumented and
            # unverified (same risk category as the K/DEF bucket stats this
            # file already declines to guess at, above). Rather than parse
            # an unconfirmed shape and risk silently returning a player's
            # SEASON total mislabeled as their WEEK's projection -- exactly
            # the "confidently wrong" anti-goal -- refuse outright and let
            # the normal degrade / last-known-good machinery (R2/R3) take
            # over honestly. Season-long fetches (week=None) are unaffected
            # and remain verified (Milestone 0).
            raise ProjectionsFetchError("ESPN weekly projections are not a verified data shape; refusing to guess")

        espn_id_to_sleeper_id = {
            str(p["espn_id"]): pid for pid, p in players_raw.items() if p.get("espn_id")
        }
        wanted_position_ids = {
            espn_pos_id for espn_pos_id, pos in ESPN_POSITION_ID_MAP.items() if pos in positions
        }

        url = f"{self.espn_host}/apis/v3/games/ffl/seasons/{season}/players"
        params = {"view": "kona_player_info"}
        filter_header = {
            "players": {
                "limit": 3000,
                "filterStatsForTopScoringPeriodIds": {"value": 2, "additionalValue": [f"00{season}", f"10{season}"]},
            }
        }
        headers = dict(USER_AGENT_HEADERS, **{"X-Fantasy-Filter": json.dumps(filter_header)})
        try:
            response = requests.get(url, params=params, headers=headers, timeout=self.timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ProjectionsFetchError(str(exc)) from exc

        data = response.json()
        if not isinstance(data, list):
            raise ProjectionsFetchError(f"unexpected response shape: {type(data)}")

        raw_stats_by_player = {}
        for player in data:
            if player.get("defaultPositionId") not in wanted_position_ids:
                continue
            sleeper_id = espn_id_to_sleeper_id.get(str(player.get("id")))
            if sleeper_id is None:
                continue
            season_stats = next(
                (s for s in player.get("stats", []) if s.get("statSourceId") == 1 and s.get("statSplitTypeId") == 0),
                None,
            )
            if season_stats is None:
                continue
            decoded = {
                ESPN_STAT_MAP[int(stat_id)]: value
                for stat_id, value in season_stats.get("stats", {}).items()
                if int(stat_id) in ESPN_STAT_MAP
            }
            raw_stats_by_player[sleeper_id] = decoded

        return raw_stats_by_player

    def _fetch_fantasypros(self, position):
        url = f"{self.fantasypros_host}/nfl/projections/{position.lower()}.php"
        try:
            response = requests.get(url, params={"week": "draft"}, headers=USER_AGENT_HEADERS, timeout=self.timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ProjectionsFetchError(str(exc)) from exc

        return _parse_fantasypros_table(response.text, position)

    def _validate(self, raw, known_player_ids):
        warnings = []
        size = len(raw)
        if not (MIN_PAYLOAD_SIZE <= size <= MAX_PAYLOAD_SIZE):
            warnings.append(f"payload size {size} outside expected range [{MIN_PAYLOAD_SIZE}, {MAX_PAYLOAD_SIZE}]")
            return False, warnings

        checked = 0
        violations = 0
        for stats in raw.values():
            for key, bound in STAT_BOUNDS.items():
                if key in stats:
                    checked += 1
                    if stats[key] < 0 or stats[key] > bound:
                        violations += 1
        if checked and violations / checked > MAX_BOUND_VIOLATION_FRACTION:
            warnings.append(f"{violations}/{checked} stat values outside plausible bounds")
            return False, warnings

        if known_player_ids:
            covered = sum(
                1 for pid in known_player_ids if pid in raw and any(raw[pid].get(k, 0) for k in raw[pid])
            )
            coverage = covered / len(known_player_ids)
            if coverage < MIN_COVERAGE_FRACTION:
                warnings.append(f"coverage {coverage:.0%} of known players below {MIN_COVERAGE_FRACTION:.0%} threshold")
                return False, warnings

        return True, warnings


def _parse_fantasypros_table(html, position):
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", id="data")
    if table is None:
        return []

    thead = table.find("thead")
    header_rows = thead.find_all("tr")
    category_cells = header_rows[0].find_all("td")
    categories = []
    for cell in category_cells:
        colspan = int(cell.get("colspan", 1))
        categories.extend([cell.get_text(strip=True)] * colspan)
    subcols = [th.get_text(strip=True) for th in header_rows[1].find_all("th")]

    column_labels = ["Player"]
    for i in range(1, len(subcols)):
        category = categories[i] if i < len(categories) else ""
        column_labels.append(f"{category} {subcols[i]}".strip())

    entries = []
    for row in table.find("tbody").find_all("tr"):
        cells = row.find_all("td")
        if not cells:
            continue
        link = cells[0].find("a", class_="player-name")
        if link is None:
            continue
        name = link.get("fp-player-name") or link.get_text(strip=True)
        team = cells[0].get_text(strip=True).replace(link.get_text(strip=True), "").strip()

        raw_stats = {}
        for i in range(1, len(cells)):
            label = column_labels[i] if i < len(column_labels) else None
            raw_key = FANTASYPROS_COLUMN_TO_RAW_KEY.get(label)
            if raw_key is None:
                continue
            text = cells[i].get_text(strip=True).replace(",", "")
            try:
                raw_stats[raw_key] = float(text)
            except ValueError:
                pass

        entries.append(ConsensusEntry(name=name, team=team, position=position, raw_stats=raw_stats))

    return entries[:10]  # only the top 10 are ever real (Milestone 0) -- defensive cap


def _normalize_name(name):
    # Fold accents before stripping punctuation, or the strip silently eats
    # the letter itself: "Piñeiro" -> "pieiro", which matches nothing, while
    # Sleeper stores the transliterated "pineiro". Decomposing to NFKD splits
    # "ñ" into "n" + combining tilde so only the mark is discarded. This was
    # the sole unmatched entry out of 266 in the ADP feed.
    name = unicodedata.normalize("NFKD", name)
    name = "".join(ch for ch in name if not unicodedata.combining(ch))
    name = re.sub(r"\b(Jr\.?|Sr\.?|II|III|IV|V)\b", "", name, flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _match_fantasypros_player(name, team, position, players_raw):
    """Name+team+position match against the Sleeper player directory.
    Ambiguous or absent matches return None rather than a guess (PRD's
    "confidently wrong" anti-goal applies to bad ID matches same as bad
    numbers)."""
    normalized = _normalize_name(name)
    candidates = [
        pid
        for pid, p in players_raw.items()
        if p.get("position") == position
        and p.get("team") == team
        and (p.get("search_full_name") == normalized or _normalize_name(p.get("full_name") or "") == normalized)
    ]
    return candidates[0] if len(candidates) == 1 else None
