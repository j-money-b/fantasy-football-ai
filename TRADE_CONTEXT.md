# Trade Context — Week 3, 2026

Written 2026-09-23 to hand off to a fresh session. Multiple offers are open at
once and they interact, so they need judging **in totality**, not one at a time.

Read `CLAUDE.md` and `PRD.md` first. Everything below is state, not spec.

---

## The situation

**Jayden Daniels (QB, WAS) has a dislocated elbow.** Sleeper: `injury_status=Out`,
`injury_body_part=Elbow`, `injury_notes=Dislocated`. He is the only QB on the
roster, so the Week 3 starting lineup had a **0.0 at quarterback**. Timeline is
unknown — this is a throwing-arm injury for a QB, so "back in a week" is
optimistic and no tool here knows a return date.

**Every manager in the league is asking about Breece Hall.** That is the fact
that should drive strategy: when the whole league wants one player, the value is
in the competition between them, not in any single offer.

## My roster (Week 3 projections, season points / VORP)

| Pos | Player | Wk3 | Season | VORP | Note |
|:--|:--|--:|--:|--:|:--|
| QB | Jayden Daniels | 0.0 | 308.7 | +12.2 | **Out — dislocated elbow** |
| RB | Bijan Robinson | 20.0 | 324.9 | +150.1 | bye 11 |
| RB | Ashton Jeanty | 15.0 | 253.2 | +78.4 | bye 13 |
| RB | Breece Hall | 14.9 | 211.0 | +36.2 | bye 13 — **everyone wants him** |
| RB | Chuba Hubbard | 14.2 | 147.9 | −26.9 | bye 5 — only Wk13 cover |
| RB | Rico Dowdle | 12.2 | 161.1 | −13.7 | Questionable (toe), bye 9 |
| WR | Drake London | 13.5 | 250.2 | +54.8 | |
| WR | Emeka Egbuka | 10.1 | 224.0 | +28.6 | bye 10 |
| WR | Courtland Sutton | 10.4 | 174.3 | −21.1 | bye 10 |
| WR | Michael Pittman | 9.2 | 170.9 | −24.5 | Questionable |
| WR | Jakobi Meyers | 8.3 | 169.3 | −26.1 | Questionable |
| TE | Tyler Warren | 10.0 | 201.1 | +37.5 | only TE |
| K | Ka'imi Fairbairn | 6.9 | | | |
| DEF | Detroit Lions | 8.3 | | | |

**Shape:** RB-rich (five, three genuinely good), WR-thin past London, one TE,
no backup QB. **$100 FAAB completely unspent** in Week 3.

**Bye-week trap:** Jeanty *and* Hall are both out Week 13. Hubbard (bye 5) is the
only cover. Trading Hall and Hubbard in separate deals would leave Week 13 as
Bijan plus a Questionable Dowdle.

## Offer 1 — peetypablo: Dak Prescott for Chuba Hubbard

**Recommendation given: counter, do not accept straight.**

- Dak beats the best free-agent QB by ~1 pt/week and ~8 season points vs Bo Nix
- The wire has **Kyler Murray** (17.6 wk / 283.1 szn, $20–35), **Bo Nix**
  (17.1 / 295.7, $8–20), **Justin Herbert** (16.6 / 295.5, $2–8)
- So QB is solvable with money, not assets — and the money is otherwise wasted
- Hubbard is the only Week 13 bye cover

**Their roster is in trouble, which is leverage:**

| Pos | Player | Wk3 | Status |
|:--|:--|--:|:--|
| QB | Dak Prescott | 18.6 | their **only** QB |
| RB | Chase Brown | 16.1 | |
| RB | TreVeyon Henderson | 12.7 | |
| RB | Josh Jacobs | **0.0** | **NA — Personal, cannot play** |
| RB | Kyle Monangai | 7.0 | Questionable |
| TE | Brock Bowers | 13.6 | Questionable — **meniscus surgery** |
| TE | Mark Andrews | 11.7 | 162.5 szn, VORP −1.1 — surplus |
| TE | Isaiah Likely | 9.7 | 157.3 szn, VORP −6.3 — surplus |
| WR | Amon-Ra St. Brown | 19.6 | 280.5 szn, VORP +85.1 — their best |
| WR | Zay Flowers | 16.9 | Questionable (hamstring) |

They have **two functional RBs**, three TEs and one QB. They need RBs badly.

**Counter proposed:** Hubbard for **Dak + Mark Andrews** (Andrews is pure TE
surplus; we run one TE). Softer fallback: Likely instead of Andrews.
**Hubbard-for-Bowers is a non-starter** — ~90 VORP in one direction.

**Regardless of the trade:** claim **Bo Nix at $8–20**. Best season value of the
free QBs, and insurance behind a dislocated elbow.

## Offers 2+ — not yet reviewed

Several more open, most involving **Breece Hall**. Not yet evaluated.

**How to approach them:**
1. Get every offer on the table before accepting any one — they compete
2. Hall is the scarce asset; make managers bid against each other
3. Trade **from** RB surplus **into** WR/TE need — but never both Hall and
   Hubbard, or Week 13 collapses
4. Check every player's `injury_status` before valuing them (see below)
5. QB is already solved by waivers; do not pay a player for it

## Tooling state

```
ffai trade --send "<name>" --receive "<name>" --with "<manager display name>"
ffai waivers        # ranked targets + FAAB bid ranges
ffai brief          # full weekly brief
```

**Known limits of `ffai trade` — read before trusting its output:**

- **Verdict is void when anyone in the deal cannot play.** It now says
  `CANNOT BE JUDGED ON PROJECTIONS ALONE` and reports points-per-game rather
  than inventing a return date.
- **Unavailable players still starting post-trade are flagged** as inflating
  that side's lineup total.
- **`lineup_delta` treats the season as one static lineup** — a deliberate
  simplification (see `trade.py` docstring). It assumes every player is healthy
  all year, which is why Daniels reads 308.7 despite the elbow.
- **A huge negative `lineup_delta` on the other side usually means they'd have
  an unfillable slot**, not that the deal is good for me. peetypablo showed
  −303.9 purely because trading Dak leaves them with zero QBs.
- **It cannot generate offers**, only evaluate ones handed to it.
- **No matchup/opponent-defense analysis exists anywhere yet.**

## Open build items

1. Trade *suggestion* — scan all 9 rosters for mutually beneficial swaps
2. Matchup-specific analysis — needs an opponent-defense data source the
   projections adapter does not provide
3. Fold availability-awareness into the brief's trade section once that exists

## Delivery (working as of 2026-09-23)

The brief is emailed as styled HTML via Gmail SMTP, Sundays and Tuesdays ~9am ET,
with the GitHub issue thread as automatic fallback. Secrets `SMTP_HOST`,
`SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `BRIEF_TO` are set in the repo.
