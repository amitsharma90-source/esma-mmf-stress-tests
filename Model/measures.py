"""Position-level shocks and monetary impacts.

Formulas are para 50 (liquidity), 51-54 (credit), 55 (interest rates) and 57 (spreads).
Two conventions run through all of them:

  modified duration = Macaulay duration / (1 + yield)
  impact            = -MV x modified duration x shock       (a loss is negative)

A measure that is out of scope for an instrument returns None, never zero. That distinction
matters: a reverse repo genuinely has no bid-ask discount, whereas a zero would silently
claim it was measured and found to be nil.
"""

from __future__ import annotations

from dataclasses import dataclass

from .loader import Fund, Position
from .reference import Reference


@dataclass
class PositionMeasures:
    ldf: float | None
    mif: float | None
    lst01_shock: float | None
    lst01_shock_mv: float | None
    liquidity_lst01: float | None
    liquidity_lst01_mst: float | None
    cst01_shock: float | None
    cst02_lgd: float | None
    ist01_shock: float | None
    credit_cst01: float | None
    credit_cst02: float | None
    interest_rates_ist01: float | None
    spread_sst01: float | None
    fx_fst01: float
    fx_fst02: float
    weekly_tradable: float
    wla_bucket: str
    wla_weight: float
    sources: dict


def _modified(duration: float, yield_: float) -> float:
    """Macaulay to modified. The (1+y) divisor is a compounding convention, not a shock."""
    return duration / (1.0 + yield_)


def measure(p: Position, fund: Fund, ref: Reference, redemption_rate: float,
            macro_redemption_rate: float) -> PositionMeasures:
    from .classify import wla_bucket

    src: dict[str, str] = {}

    ldf_s = ref.liquidity_discount(p.instrument, p.issuer_type, p.issuer_country,
                                   p.lt_rating, p.maturity_bucket)
    src["ldf"] = ldf_s.source

    # para 50: the fund sells a vertical slice, and its own selling depresses the price.
    # The design doc carried this twice, as LST01Shock_MIF and PIF, because its source
    # system reported a value alongside the computed one and the two differed slightly.
    # There is one computation here, so there is one column.
    pip_s = ref.price_impact(p.instrument, p.sector)
    src["pip"] = pip_s.source
    asset_sales = p.market_value * redemption_rate
    mif = None if pip_s.value is None else pip_s.value * asset_sales

    if ldf_s.value is None and mif is None:
        lst01 = None
    else:
        lst01 = (ldf_s.value or 0.0) + (mif or 0.0)

    liquidity_lst01 = None if lst01 is None else -p.market_value * lst01
    # The macro scenario sells less, so only the price-impact half changes.
    macro_sales = p.market_value * macro_redemption_rate
    macro_mif = None if pip_s.value is None else pip_s.value * macro_sales
    if ldf_s.value is None and macro_mif is None:
        liquidity_mst = None
    else:
        liquidity_mst = -p.market_value * ((ldf_s.value or 0.0) + (macro_mif or 0.0))

    cst01_s = ref.credit_spread(p.instrument, p.issuer_type, p.sector, p.issuer_country,
                                p.lt_rating, p.maturity_bucket)
    src["cst01"] = cst01_s.source
    credit_cst01 = (None if cst01_s.value is None
                    else -p.market_value * _modified(p.spread_duration, p.yield_) * cst01_s.value)

    lgd_s = ref.loss_given_default(p.instrument, p.seniority)
    src["cst02"] = lgd_s.source
    # Default is binary, so no duration adjustment applies.
    credit_cst02 = None if lgd_s.value is None else -p.market_value * lgd_s.value

    ist01_s = ref.rate_shock(p.instrument, p.currency, p.maturity_bucket)
    src["ist01"] = ist01_s.source
    interest_ist01 = (None if ist01_s.value is None
                      else -p.market_value * _modified(p.rate_duration, p.yield_) * ist01_s.value)

    # para 57: instruments tied to an index carry a benchmark spread move over their life,
    # so SST uses spread duration; anything not index-linked falls back to the rate curve
    # and therefore equals IST-01.
    if ist01_s.value is None:
        spread_sst01 = None
    elif p.rate_type == "FLOATING" and ref.settings.get("sst_sensitivity_basis") == "SPREAD_DURATION":
        spread_sst01 = -p.market_value * _modified(p.spread_duration, p.yield_) * ist01_s.value
        src["sst01"] = ist01_s.source + " on spread duration (index-linked)"
    else:
        spread_sst01 = interest_ist01
        src["sst01"] = ist01_s.source + " on rate duration (not index-linked)"

    # Change in the holding's value in the fund's currency; a gain is positive.
    fx01_s = ref.fx_value_change(p.instrument, p.currency, fund.base_currency, "APPRECIATION")
    fx02_s = ref.fx_value_change(p.instrument, p.currency, fund.base_currency, "DEPRECIATION")
    src["fst01"], src["fst02"] = fx01_s.source, fx02_s.source
    fx01, fx02 = fx01_s.value or 0.0, fx02_s.value or 0.0

    b = wla_bucket(p, ref)
    src["wla"] = b.reason

    # para 60: the weekly tradable amount is what could reasonably be liquidated inside a
    # working week, "including maturing assets". Two ways to qualify, so an instrument that
    # cannot be SOLD inside the week still counts if it MATURES inside it - a 3-day time
    # deposit turns into cash on its own however long its notice period would have been.
    # The same test drives the RST-01 tradable set, or the two would contradict each other.
    from .classify import is_weekly_tradable
    weekly_tradable = p.market_value if is_weekly_tradable(p, ref) else 0.0

    return PositionMeasures(
        ldf=ldf_s.value, mif=mif, lst01_shock=lst01,
        lst01_shock_mv=None if lst01 is None else lst01 * p.market_value,
        liquidity_lst01=liquidity_lst01, liquidity_lst01_mst=liquidity_mst,
        cst01_shock=cst01_s.value, cst02_lgd=lgd_s.value, ist01_shock=ist01_s.value,
        credit_cst01=credit_cst01, credit_cst02=credit_cst02,
        interest_rates_ist01=interest_ist01, spread_sst01=spread_sst01,
        fx_fst01=fx01, fx_fst02=fx02,
        weekly_tradable=weekly_tradable,
        wla_bucket=b.bucket, wla_weight=b.weight, sources=src,
    )


def measure_fund(fund: Fund, ref: Reference) -> dict[str, PositionMeasures]:
    """para 61 blends the two investor classes: 40% of professional and 30% of retail."""
    r = (ref.redemption["INSTITUTIONAL"] * fund.institutional_share
         + ref.redemption["RETAIL"] * fund.retail_share)
    rm = (ref.redemption_macro["INSTITUTIONAL"] * fund.institutional_share
          + ref.redemption_macro["RETAIL"] * fund.retail_share)
    return {p.security_id: measure(p, fund, ref, r, rm) for p in fund.positions}


def blended_redemption(fund: Fund, ref: Reference, macro: bool = False) -> float:
    table = ref.redemption_macro if macro else ref.redemption
    return (table["INSTITUTIONAL"] * fund.institutional_share
            + table["RETAIL"] * fund.retail_share)
