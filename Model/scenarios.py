"""The twelve portfolio scenarios.

Impact convention is para 58: (Reporting NAV - Stressed NAV) / Reporting NAV. A loss reports
positive and a gain negative. The sign is kept, never folded with abs(), so a currency move
that raises a fund's value cannot read as a loss. Para 64's macro formula is loosely worded
and reads like the surviving ratio; para 58 governs and is applied consistently.

Coverage scenarios (RST-02, RST-03, MST-02) report two figures, as paras 61, 62 and 65 do:
first bucket is bucket 1 alone, total bucket adds bucket 2 at its para 61 weight.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import optimise
from .classify import is_weekly_tradable, weekly_liquid_assets  # noqa: F401  is_weekly_tradable re-exported
from .loader import Fund
from .measures import PositionMeasures, blended_redemption
from .reference import Reference

FX_ATTR = {"APPRECIATION": "fx_fst01", "DEPRECIATION": "fx_fst02"}


@dataclass
class ScenarioResult:
    risk_factor: str
    code: str
    impact_on_nav: float | None = None
    first_bucket: float | None = None
    total_bucket: float | None = None
    note: str = ""


def _sum(measures: dict[str, PositionMeasures], attr: str) -> float:
    return sum(getattr(m, attr) or 0.0 for m in measures.values())


def impact(pnl: float, nav: float) -> float:
    """para 58 on a P&L that is negative for a loss: the loss reads positive.

    Adding 0.0 turns the -0.0 of a nil P&L into 0.0, which would otherwise print as -0.00%.
    """
    return -pnl / nav + 0.0


def fx_pnl(fund: Fund, measures: dict[str, PositionMeasures], direction: str) -> float:
    attr = FX_ATTR[direction]
    return sum(getattr(measures[p.security_id], attr) * p.market_value for p in fund.positions)


def adverse_fx(fund: Fund, measures: dict[str, PositionMeasures]) -> tuple[str, float]:
    """para 65's adverse FX shock: whichever of Table 10 and Table 11 costs the fund more.

    One direction for the whole fund. The two tables are alternative states of the world,
    so each holding cannot pick its own worse side.
    """
    legs = {d: fx_pnl(fund, measures, d) for d in FX_ATTR}
    leg = min(legs, key=legs.get)
    return leg, legs[leg]


def market_shock(fund: Fund, measures: dict[str, PositionMeasures]) -> float:
    """para 63 and 65: rates + credit + the adverse side of FX, as a P&L."""
    _leg, fx = adverse_fx(fund, measures)
    return _sum(measures, "interest_rates_ist01") + _sum(measures, "credit_cst01") + fx


def stressed_values(fund: Fund, measures: dict[str, PositionMeasures]) -> dict[str, float]:
    """Each holding revalued by its own rate, credit and adverse FX legs (para 63)."""
    leg, _fx = adverse_fx(fund, measures)
    attr = FX_ATTR[leg]
    out = {}
    for p in fund.positions:
        m = measures[p.security_id]
        out[p.security_id] = (p.market_value + (m.interest_rates_ist01 or 0.0)
                              + (m.credit_cst01 or 0.0) + getattr(m, attr) * p.market_value)
    return out


def run(fund: Fund, ref: Reference, measures: dict[str, PositionMeasures],
        rst01: optimise.RST01Result | None = None) -> list[ScenarioResult]:
    nav = fund.nav
    wla = weekly_liquid_assets(fund, ref)
    redemption = blended_redemption(fund, ref)
    macro_redemption = blended_redemption(fund, ref, macro=True)
    w1 = ref.bucket_weight(1)

    def coverage(outflow: float, assets: dict | None = None) -> tuple[float, float]:
        a = assets or wla
        if outflow <= 0:
            return 0.0, 0.0
        return a["bucket_1"] * w1 / outflow, a["total"] / outflow

    out: list[ScenarioResult] = []

    # -- market risk factors, para 58 -------------------------------------
    out.append(ScenarioResult("Liquidity", "LST-01",
                              impact_on_nav=impact(_sum(measures, "liquidity_lst01"), nav),
                              note=f"vertical slice at {redemption:.0%} of NAV"))
    out.append(ScenarioResult("Credit", "CST-01",
                              impact_on_nav=impact(_sum(measures, "credit_cst01"), nav)))

    # para 53 scope: the two counterparties producing the LARGEST LOSS, which is not the
    # same as the two largest exposures once repos and cash are excluded from credit.
    by_issuer: dict[str, float] = {}
    for p in fund.positions:
        loss = measures[p.security_id].credit_cst02
        if loss:
            by_issuer[p.issuer] = by_issuer.get(p.issuer, 0.0) + loss
    worst = sorted(by_issuer.items(), key=lambda kv: kv[1])[:2]
    out.append(ScenarioResult("Credit", "CST-02",
                              impact_on_nav=impact(sum(v for _k, v in worst), nav),
                              note="worst two by loss: " + ", ".join(k for k, _v in worst)))

    out.append(ScenarioResult("FX rate", "FST-01",
                              impact_on_nav=impact(fx_pnl(fund, measures, "APPRECIATION"), nav),
                              note="Table 10, EUR appreciation against the USD"))
    out.append(ScenarioResult("FX rate", "FST-02",
                              impact_on_nav=impact(fx_pnl(fund, measures, "DEPRECIATION"), nav),
                              note="Table 11, EUR depreciation against the USD"))
    out.append(ScenarioResult("Interest rate", "IST-01",
                              impact_on_nav=impact(_sum(measures, "interest_rates_ist01"), nav)))

    # -- redemption, paras 60 to 62 ---------------------------------------
    rst01 = rst01 or optimise.solve(fund, ref)
    out.append(ScenarioResult("Level of redemption", "RST-01",
                              impact_on_nav=rst01.fraction, note=rst01.note))

    first, total = coverage(redemption * nav)
    out.append(ScenarioResult("Level of redemption", "RST-02", first_bucket=first,
                              total_bucket=total,
                              note=f"outflow {redemption:.0%} of NAV (para 61)"))

    first, total = coverage(fund.top2_investor_share * nav)
    out.append(ScenarioResult("Level of redemption", "RST-03", first_bucket=first,
                              total_bucket=total,
                              note=f"two main investors hold {fund.top2_investor_share:.2%} (para 62)"))

    out.append(ScenarioResult("Spread among indices", "SST-01",
                              impact_on_nav=impact(_sum(measures, "spread_sst01"), nav),
                              note="index-linked positions carry the shock on spread duration"))

    # -- macro, paras 63 to 65 --------------------------------------------
    # para 63 lists the market shock as FX + interest rate + credit + spread among indices;
    # para 65 adds that it combines "an adverse FX shock and an increase in interest rates
    # including swap rate, government bond yields and corporate bond yields", with "the
    # credit risk included in the yield shock". So: rates + credit + the ADVERSE side of FX.
    # SST is deliberately left out - for fixed-rate paper it equals IST exactly, so adding
    # it would double count the same curve move.
    shock = market_shock(fund, measures)
    # The market shock hits the whole portfolio; the liquidity cost applies only to the
    # slice actually sold.
    liquidity_cost = macro_redemption * _sum(measures, "liquidity_lst01_mst")
    out.append(ScenarioResult("Macro", "MST-01",
                              impact_on_nav=impact(shock + liquidity_cost, nav),
                              note=f"market shock plus liquidity cost on {macro_redemption:.0%} sold"))

    # para 63: "the value of weekly liquid assets AFTER market shock as a percentage of
    # outflows". Both sides of the ratio are post-shock - revalue each position by its own
    # rate, credit and adverse FX legs, then re-sum the buckets.
    stressed_nav = nav + shock
    stressed_wla = weekly_liquid_assets(fund, ref, stressed_values(fund, measures))
    first, total = coverage(macro_redemption * stressed_nav, stressed_wla)
    out.append(ScenarioResult("Macro", "MST-02", first_bucket=first, total_bucket=total,
                              note=f"post-shock weekly liquid assets over {macro_redemption:.0%} "
                                   f"of post-shock NAV (paras 63 and 65)"))

    return out
