from ffai.cache import Cache
from ffai.config import LEAGUE_ID
from ffai.repository import fetch_league
from ffai.sleeper_client import SleeperClient


def main():
    client = SleeperClient()
    cache = Cache()
    league, stale, fetched_at = fetch_league(client, cache, LEAGUE_ID)

    if stale:
        print(f"WARNING: live fetch failed, showing cached data from {fetched_at}\n")

    scoring_settings = league.get("scoring_settings", {})
    scoring_type = "PPR" if scoring_settings.get("rec") == 1 else (
        "Half-PPR" if scoring_settings.get("rec") == 0.5 else "Standard"
    )

    print(f"League Name: {league.get('name')}")
    print(f"Season: {league.get('season')}")
    print(f"Total Rosters: {league.get('total_rosters')}")
    print(f"Scoring Type: {scoring_type}")
    print(f"Roster Positions: {', '.join(league.get('roster_positions', []))}")


if __name__ == "__main__":
    main()
