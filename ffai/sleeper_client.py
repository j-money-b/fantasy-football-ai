import requests

from ffai.config import SLEEPER_BASE_URL

REQUEST_TIMEOUT_SECONDS = 10


class SleeperAPIError(Exception):
    """Raised when a Sleeper API call fails or returns an unexpected response."""


class SleeperClient:
    """Thin wrapper around the Sleeper API. No caching, no fallback -- just HTTP."""

    def __init__(self, base_url: str = SLEEPER_BASE_URL):
        self.base_url = base_url

    def get_league(self, league_id: str) -> dict:
        url = f"{self.base_url}/league/{league_id}"
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise SleeperAPIError(f"Failed to fetch league {league_id}: {exc}") from exc

        data = response.json()
        if not isinstance(data, dict):
            raise SleeperAPIError(f"Unexpected response fetching league {league_id}: {data!r}")

        return data

    def get_players(self) -> dict:
        """Full NFL player dictionary (~5MB), keyed by player_id. PRD 3.1: pull
        at most once daily via repository.fetch_players, not per-request."""
        url = f"{self.base_url}/players/nfl"
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise SleeperAPIError(f"Failed to fetch player dictionary: {exc}") from exc

        data = response.json()
        if not isinstance(data, dict):
            raise SleeperAPIError(f"Unexpected response fetching player dictionary: {data!r}")

        return data

    def get_draft(self, draft_id: str) -> dict:
        """Draft metadata: status, settings, roster_positions, draft_order, etc."""
        url = f"{self.base_url}/draft/{draft_id}"
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise SleeperAPIError(f"Failed to fetch draft {draft_id}: {exc}") from exc

        data = response.json()
        if not isinstance(data, dict):
            raise SleeperAPIError(f"Unexpected response fetching draft {draft_id}: {data!r}")

        return data

    def get_draft_picks(self, draft_id: str) -> list:
        """All picks made so far in the draft, in pick_no order."""
        url = f"{self.base_url}/draft/{draft_id}/picks"
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise SleeperAPIError(f"Failed to fetch picks for draft {draft_id}: {exc}") from exc

        data = response.json()
        if not isinstance(data, list):
            raise SleeperAPIError(f"Unexpected response fetching picks for draft {draft_id}: {data!r}")

        return data

    def get_stats(self, season: str, week: int) -> dict:
        """Actual (not projected) per-player raw stats for one week. Keyed by
        player_id for individual players; team defenses use a "TEAM_XXX" key."""
        url = f"{self.base_url}/stats/nfl/regular/{season}/{week}"
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise SleeperAPIError(f"Failed to fetch stats for {season} week {week}: {exc}") from exc

        data = response.json()
        if not isinstance(data, dict):
            raise SleeperAPIError(f"Unexpected response fetching stats for {season} week {week}: {data!r}")

        return data
