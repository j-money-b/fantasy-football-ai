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


@dataclass
class LineupSlot:
    slot: str  # roster slot label, e.g. "QB", "RB", "FLEX" -- one entry per starting slot, duplicates preserved
    player: "PlayerProjection | None" = None


@dataclass
class LineupResult:
    slots: list  # list[LineupSlot], in roster_positions order
    bench: list  # list[PlayerProjection]
    total_points: float = 0.0
