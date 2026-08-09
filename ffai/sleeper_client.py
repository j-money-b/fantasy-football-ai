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
