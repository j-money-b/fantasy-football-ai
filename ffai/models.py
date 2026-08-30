from dataclasses import dataclass, field


@dataclass
class PlayerProjection:
    player_id: str
    name: str
    position: str
    team: str
    bye_week: int | None
    points: float
    data_source: str  # "sleeper" | "espn" | "none"


@dataclass
class PlayerVorp(PlayerProjection):
    vorp: float = 0.0
    tier: int | None = None


@dataclass
class Tier:
    tier_number: int
    players: list = field(default_factory=list)  # list[PlayerVorp]
    min_vorp: float = 0.0
    max_vorp: float = 0.0


@dataclass
class Recommendation:
    player: PlayerVorp
    reasons: list  # list[str]
    degraded: bool
    data_source: str
    alternatives: list = field(default_factory=list)  # list[str], best available per open slot


@dataclass
class LineupSlot:
    slot: str  # roster slot label, e.g. "QB", "RB", "FLEX" -- one entry per starting slot, duplicates preserved
    player: "PlayerProjection | None" = None


@dataclass
class LineupResult:
    slots: list  # list[LineupSlot], in roster_positions order
    bench: list  # list[PlayerProjection]
    total_points: float = 0.0


@dataclass
class WaiverTarget:
    player: PlayerProjection
    marginal_value: float  # points this player would add to my optimal lineup right now, vs. today
    trending_count: "int | None"
    reasons: list  # list[str]


@dataclass
class TradeParty:
    label: str
    roster_player_ids: list  # every player_id currently on this team's roster
    sends_ids: list  # player_ids this team would send away in the proposed trade


@dataclass
class TradeSideResult:
    label: str
    sends: list  # list[PlayerVorp]
    receives: list  # list[PlayerVorp]
    value_sent: float  # summed season VORP of players sent away
    value_received: float  # summed season VORP of players received
    net_vorp: float  # value_received - value_sent
    lineup_delta: float  # change in this team's optimal season-aggregate lineup total, after vs. before
    bye_week_warnings: list  # list[str]


@dataclass
class TradeEvaluation:
    sides: list  # list[TradeSideResult], one per team
    verdict: str


@dataclass
class BenchPointsLostResult:
    week: int
    actual_points: float  # what your actually-started lineup scored, from real final stats
    optimal_points: float  # what the retrospectively optimal lineup would have scored
    bench_points_lost: float  # optimal_points - actual_points (>= 0 by construction)
    swapped_in: list  # list[PlayerProjection] -- benched players who should have started
    swapped_out: list  # list[PlayerProjection] -- started players who should have been benched
