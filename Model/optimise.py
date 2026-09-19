"""RST-01 - the largest weekly tradable sale that keeps the fund compliant at every step.

para 60 asks for the MAXIMUM weekly tradable amount a fund can liquidate while it still
complies with at least Articles 17, 18, 24 and 25. It is answered in two stages.

1. OPTIMISE where the sale ends. Write h_i for the value of holding i that is kept, scaled
   so total assets = 1, and S for the sum of everything kept. A holding that cannot be sold
   inside a week stays whole; a weekly tradable one may be kept anywhere from 0 to its full
   value. Every limit is a share of S, so each one linearises by multiplying through by S:

       minimise   sum of kept tradable value                 = sell as much as possible
       WAM, WAL   sum(h_i * (days_i - cap))              <= 0
       daily      sum(h_i * (1_daily_i - min))           >= 0
       weekly     sum(h_i * (1_weekly_i - min)) + z      >= 0
                  z <= sum(h_i * 1_inclusion_i),   z <= inclusion cap * S
       groups     sum(h_i in group) - cap * S            <= 0     every Art 16 / 17 cap

   Raising z relaxes the weekly test, so the optimum drives it to min(eligible, cap).

   Two limits are not linear. Article 17(2)'s 40% counts a body only once it is above 5%,
   which takes one yes/no variable y_j per body (VNAV funds only):
       H_j - 5% * S <= y_j,     A_j >= H_j - (1 - y_j),     sum(A_j) <= 40% * S
   Article 17(7)(a)'s six issues is a count: an issuer held today in fewer than six issues
   is kept at or below 5%; one held in six or more is left to stage 2.
   Article 18 needs no row - selling only lowers a fund's share of an issuer.

2. PROVE the fund gets there compliant. Holdings move in a straight line from today's to
   the end state, and portfolio_ratios() - the same judge used at rest - re-tests the fund
   at rst01_path_steps evenly spaced points. A limit that is linear in the holdings cannot
   break between two compliant points, so the test is aimed at the two that are not. If a
   point fails, RST-01 is cut back to the last compliant point before it.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from .classify import (body_limit_label, counts_toward_inclusion, inclusion_cap, is_daily_liquid,
                       is_weekly_liquid, is_weekly_tradable, pct, portfolio_ratios,
                       public_debt_issues, share_limits)
from .loader import Fund, Position
from .reference import Reference, ReferenceError

# Numerical, not regulatory. Inside the solver every limit is tightened by this relative
# amount, so a sale that lands exactly on a limit is not read back as a hair over it.
SOLVER_MARGIN = 1e-7


@dataclass
class RST01Result:
    fraction: float                  # RST-01: share of total assets sold
    ceiling: float                   # weekly tradable amount, share of total assets
    solver_fraction: float           # where the optimiser ends the sale, before the path test
    sold: dict[str, float]           # security_id -> amount sold at the optimiser's end state
    path_steps: int
    path_reached: float              # last compliant point on the path, 0 to 1
    binding: list[str] = field(default_factory=list)
    status: str = ""
    note: str = ""

    def sold_at_rst01(self) -> dict[str, float]:
        """Amount sold per holding at the reported RST-01 point."""
        return {k: v * self.path_reached for k, v in self.sold.items()}


def residual_after(fund: Fund, sold: dict[str, float], t: float) -> list[Position]:
    """The portfolio left t of the way along the straight path to selling `sold`."""
    floor = fund.total_assets * 1e-12
    out = []
    for p in fund.positions:
        keep = p.market_value - t * sold.get(p.security_id, 0.0)
        if keep <= floor:
            continue
        clone = copy.copy(p)
        clone.market_value = keep
        out.append(clone)
    return out


def solve(fund: Fund, ref: Reference) -> RST01Result:
    method = ref.settings.get("rst01_method")
    if method != "LARGEST_COMPLIANT_SALE":
        raise ReferenceError(f"rst01_method {method!r} is not supported")
    steps = int(ref.settings["rst01_path_steps"])

    positions = list(fund.positions)
    total = fund.total_assets
    limits = ref.limits(fund.fund_type, fund.nav_type)
    trade = [i for i, p in enumerate(positions) if is_weekly_tradable(p, ref)]
    ceiling = sum(positions[i].market_value for i in trade) / total if total > 0 else 0.0
    if not trade:
        return RST01Result(0.0, 0.0, 0.0, {}, steps, 0.0, status="nothing tradable",
                           note="no position can be liquidated within one week")
    at_rest = portfolio_ratios(fund, ref)
    if not at_rest["compliant"]:
        return RST01Result(0.0, ceiling, 0.0, {}, steps, 0.0, list(at_rest["breaches"]),
                           "non-compliant at rest", "the fund does not comply before any sale")

    # ---- 1. optimise ----------------------------------------------------------------
    size = len(positions)
    mv = np.array([p.market_value / total for p in positions])
    is_trade = np.zeros(size, dtype=bool)
    is_trade[trade] = True
    fixed = np.where(is_trade, 0.0, mv)
    ones = np.ones(size)
    tight, loose = 1.0 - SOLVER_MARGIN, 1.0 + SOLVER_MARGIN

    groups = share_limits(positions, ref, limits)
    base = limits["art17_single_body_base_cap"]
    aggregate = limits["art17_diversification_aggregate"]
    body = body_limit_label(limits)
    bodies = [g for g in groups if g.article == body] if aggregate is not None else []
    n, m = len(trade), len(bodies)
    Z, Y0, A0, nv = n, n + 1, n + 1 + m, n + 1 + 2 * m

    rows: list[np.ndarray] = []
    upper: list[float] = []
    labels: list[str | None] = []

    def le(label, coeff, extra=None, rhs=0.0):
        """sum_i coeff_i * h_i + extra <= rhs, with whole holdings moved to the right."""
        row = np.zeros(nv)
        row[:n] = coeff[trade]
        for k, v in (extra or {}).items():
            row[k] += v
        rows.append(row)
        upper.append(rhs - float(coeff @ fixed))
        labels.append(label)

    def member(ix):
        v = np.zeros(size)
        v[list(ix)] = 1.0
        return v

    def flag(test):
        return np.array([1.0 if test(p) else 0.0 for p in positions])

    wam_cap, wal_cap = limits["wam_cap_days"], limits["wal_cap_days"]
    le(f"WAM at its {wam_cap}-day cap", np.array([p.wam_days for p in positions]) - wam_cap * tight)
    le(f"WAL at its {wal_cap}-day cap", np.array([p.wal_days for p in positions]) - wal_cap * tight)
    dmin, wmin = limits["daily_liquid_min"], limits["weekly_liquid_min"]
    le(f"daily liquidity at its {pct(dmin)} minimum",
       -(flag(lambda p: is_daily_liquid(p, ref, limits)) - dmin * loose))
    le(f"weekly liquidity at its {pct(wmin)} minimum",
       -(flag(lambda p: is_weekly_liquid(p, ref, limits)) - wmin * loose), {Z: -1.0})
    cap = inclusion_cap(limits)
    incl = flag(lambda p: counts_toward_inclusion(p, ref, limits))
    if cap is not None:
        le(None, -incl, {Z: 1.0})
        le(None, -cap * tight * ones, {Z: 1.0})

    for g in groups:
        le(f"{g.article} {g.name} at its {pct(g.cap)} cap", member(g.members) - g.cap * tight * ones)

    for issuer, ix in public_debt_issues(positions, ref).items():
        if len(ix) < limits["art17_7_min_issues"]:
            le(f"Art 17(7) {issuer} held to {pct(base)} with fewer than "
               f"{limits['art17_7_min_issues']} issues", member(ix) - base * tight * ones)

    for k, g in enumerate(bodies):
        h = member(g.members)
        le(None, h - base * tight * ones, {Y0 + k: -1.0})
        le(None, h, {A0 + k: -1.0, Y0 + k: 1.0}, rhs=1.0)
    if bodies:
        le(f"Art 17(2) bodies above {pct(base)} at the {pct(aggregate)} aggregate",
           -aggregate * tight * ones, {A0 + k: 1.0 for k in range(m)})

    objective = np.zeros(nv)
    objective[:n] = 1.0
    ub = np.concatenate([mv[trade], [np.inf if cap is not None else 0.0],
                         np.ones(m), np.full(m, np.inf)])
    integrality = np.concatenate([np.zeros(n + 1), np.ones(m), np.zeros(m)])
    matrix = np.array(rows)
    res = milp(objective, integrality=integrality, bounds=Bounds(np.zeros(nv), ub),
               constraints=LinearConstraint(matrix, -np.inf, np.array(upper)))
    if res.status != 0 or res.x is None:
        # Keeping every holding is feasible for a fund compliant at rest, so a failure here
        # is a defect in the model, not an answer.
        raise RuntimeError(f"{fund.code}: RST-01 optimiser did not solve - {res.message}")

    kept = np.clip(res.x[:n], 0.0, mv[trade])
    sold_scaled = mv[trade] - kept
    solver_fraction = float(sold_scaled.sum())
    sold = {positions[i].security_id: float(sold_scaled[k] * total) for k, i in enumerate(trade)}

    # Which limits stop the sale. The helper variables may sit anywhere their rows allow, so
    # the solver's own values say nothing about whether a limit binds - z can stop short of
    # the inclusion the fund is entitled to and make weekly liquidity look tight. Each is
    # reset to the value the kept holdings imply before any slack is read.
    held = fixed.copy()
    held[trade] = kept
    kept_total = float(held.sum())
    x = res.x.copy()
    x[:n] = kept
    if cap is not None:
        x[Z] = min(float(incl @ held), cap * tight * kept_total)
    # A body counts toward the Article 17(2) aggregate only once it is above 5%. One held
    # exactly AT 5% is not in the total, yet it is what stops the sale when letting it cross
    # would push the total past 40% - so it is named on its own rather than as the total.
    above_total, at_threshold = 0.0, []
    for k, g in enumerate(bodies):
        h_j = float(member(g.members) @ held)
        if h_j > base * kept_total * (1.0 + 1e-6):
            x[Y0 + k], x[A0 + k] = 1.0, h_j
            above_total += h_j
        else:
            x[Y0 + k], x[A0 + k] = 0.0, 0.0
            if h_j >= base * tight * kept_total * (1.0 - 1e-6):
                at_threshold.append((g.name, h_j))
    slack = np.array(upper) - matrix @ x
    scale = np.maximum(1.0, np.abs(matrix).max(axis=1))
    binding = [lab for lab, s, sc in zip(labels, slack, scale) if lab and s <= 1e-6 * sc]
    held_back = [name for name, h_j in at_threshold
                 if above_total + h_j > aggregate * kept_total * (1.0 - 1e-6)]
    if held_back:
        names = (", ".join(held_back[:-1]) + " and " + held_back[-1]) if len(held_back) > 1 else held_back[0]
        binding.append(f"Art 17(2) {names} held to {pct(base)}; any of them above it would take "
                       f"bodies over {pct(base)} past the {pct(aggregate)} aggregate")

    # ---- 2. prove the path ----------------------------------------------------------
    reached, cut = 0.0, None
    for k in range(1, steps + 1):
        t = k / steps
        residual = residual_after(fund, sold, t)
        if residual:
            r = portfolio_ratios(fund, ref, residual)
            if not r["compliant"]:
                cut = (t, r["breaches"])
                break
        reached = t
    fraction = solver_fraction * reached

    if cut is not None:
        status = "cut back on the sale path"
        binding = [cut[1][0]]
        note = (f"cut back to {fraction:.2%}: {cut[1][0]} at {cut[0]:.2%} of the way to the "
                f"optimiser's {solver_fraction:.2%}")
    elif solver_fraction >= ceiling - 1e-9:
        status, binding = "tradable ceiling", []
        note = f"limited by weekly tradable amount ({ceiling:.1%} of assets); no portfolio limit binds"
    else:
        status = "optimal"
        note = "binding constraint: " + ("; ".join(binding) if binding else "none identified")
    return RST01Result(fraction, ceiling, solver_fraction, sold, steps, reached,
                       binding, status, note)
