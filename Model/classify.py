"""Weekly liquid asset classification, and the MMF Regulation's portfolio limits.

Para 61's table is a set of alternative qualifying criteria, not one rule. A position lands
in bucket 1 if it satisfies ANY of that bucket's four lines:

    Art 17(7) public debt, settles in one working day, residual maturity <= 190d, CQS 1
    cash withdrawable on five working days notice
    weekly maturing assets                       <- no CQS test, no issuer-type test
    reverse repos terminable on five working days

which is why a corporate CP maturing inside a week reaches bucket 1 through line 3 even
though it could never satisfy line 1. Bucket 2 has three lines of its own at 85% weight.
Everything else contributes nothing.

portfolio_ratios() is the single judge of compliance - at rest, and for every residual
portfolio the RST-01 solver proposes:

    Art 24/25   WAM, WAL, daily and weekly liquidity minimums
    Art 16      units of other MMFs: per fund, and in aggregate
    Art 17      per body and the VNAV aggregate, deposits per bank, securitisations and
                ABCPs, reverse repos per counterparty, combined per body, and the
                conditions on public-debt issuers
    Art 18      share of an issuer's outstanding paper

Windows count WORKING days (loader.maturity_working_days, settlement and notice days);
the 190-day and Article 10 limits count calendar days, as their texts do.
"""

from __future__ import annotations

from dataclasses import dataclass

from .loader import Fund, Position
from .reference import Reference, ReferenceError


@dataclass(frozen=True)
class BucketResult:
    bucket: str      # "Bucket 1" | "Bucket 2" | "None"
    weight: float
    reason: str


def pct(x: float) -> str:
    """17.5% keeps its decimal; 5% does not."""
    return f"{x:.1%}" if abs(x * 100 - round(x * 100)) > 1e-9 else f"{x:.0%}"


# =============================================================================
# SHARED DEFINITIONS - every module that asks these questions asks them here
# =============================================================================
def _terminable(p: Position, ref: Reference) -> bool:
    """Reverse repos and cash can be ended early on notice; everything else runs to term."""
    return ref.asset_category(p.instrument) in ("REVERSE_REPO", "CASH")


def liquidity_window_days(p: Position, ref: Reference) -> int:
    """Working days until the holding turns into cash without selling it.

    A reverse repo or cash balance qualifies on its notice period when one is given - and a
    repo maturing before its notice would expire still counts from maturity.
    """
    if _terminable(p, ref) and p.termination_notice_days is not None:
        return min(p.termination_notice_days, p.maturity_working_days)
    return p.maturity_working_days


def is_eligible_mmi(p: Position, ref: Reference) -> bool:
    """Art 10: a money market instrument only while its residual maturity is within the
    limit, or within two years where the coupon resets inside that limit."""
    if ref.asset_category(p.instrument) != "MMI":
        return True
    if p.residual_days <= ref.mmi_max_residual_days:
        return True
    return (p.residual_days <= ref.mmi_max_extended_days
            and p.reset_days is not None and p.reset_days <= ref.mmi_max_reset_days)


def is_public_debt(p: Position, ref: Reference) -> bool:
    """Money market paper from an Article 17(7) issuer."""
    return (ref.diversification_scope(p.instrument) == "MMI"
            and ref.qualifies_article_17_7(p.instrument, p.issuer_type))


def is_daily_liquid(p: Position, ref: Reference, limits: dict) -> bool:
    """Art 24(1)(c)/(d), 25(1)(c): daily maturing assets, reverse repos terminable and cash
    withdrawable on one working day's notice."""
    return (ref.is_maturing(p.instrument)
            and liquidity_window_days(p, ref) <= limits["daily_window_working_days"])


def is_weekly_liquid(p: Position, ref: Reference, limits: dict) -> bool:
    """Art 24(1)(e)/(f), 25(1)(d): weekly maturing assets, reverse repos terminable and cash
    withdrawable on five working days' notice. Units of another MMF are redeemable, not
    maturing, so they reach weekly liquidity only through the capped route below."""
    return (ref.is_maturing(p.instrument)
            and liquidity_window_days(p, ref) <= limits["weekly_window_working_days"])


def inclusion_cap(limits: dict) -> float | None:
    """The cap on the capped route into weekly liquidity - one route per fund type."""
    if limits["art_17_7_inclusion_cap"] is not None:
        return limits["art_17_7_inclusion_cap"]
    return limits["mmi_mmf_inclusion_cap"]


def counts_toward_inclusion(p: Position, ref: Reference, limits: dict) -> bool:
    """The capped route into weekly liquidity, for holdings not already weekly liquid.

    Art 24(1)(g)  LVNAV and public debt CNAV: Article 17(7) assets, highly liquid, settled
                  within one working day, residual maturity up to 190 days.
    Art 24(1)(h)  short-term VNAV, and Art 25(1)(e) standard: money market instruments OR
                  units of other MMFs, settled within five working days. Money market
                  instruments are named first in both texts - leaving them out understated
                  a standard fund's weekly liquidity.
    """
    if is_weekly_liquid(p, ref, limits):
        return False
    if limits["art_17_7_inclusion_cap"] is not None:
        return (ref.qualifies_article_17_7(p.instrument, p.issuer_type)
                and p.residual_days <= limits["art_17_7_inclusion_max_residual_days"]
                and p.settlement_days <= limits["art_17_7_inclusion_max_settlement_days"])
    if limits["mmi_mmf_inclusion_cap"] is not None:
        return (ref.asset_category(p.instrument) in ("MMI", "MMF_UNITS")
                and is_eligible_mmi(p, ref)
                and p.settlement_days <= limits["mmi_mmf_inclusion_max_settlement_days"])
    return False


def is_weekly_tradable(p: Position, ref: Reference) -> bool:
    """para 60: liquidatable within a working week, OR maturing inside it.

    The test is the manager's LIQUIDATION assessment - "the shortest period during which
    such a position could reasonably be liquidated at or near its carrying value" - which
    the position file carries as liquidity_bucket. It is NOT settlement days: settlement is
    what para 61 tests for the weekly liquid asset buckets, and using it here would let a
    T+2 settlement convention imply a 700-day note is sellable inside a week.

    Shared with the weeklyTradableAmount column so the report cannot contradict the solver.
    """
    max_days = int(ref.settings["weekly_tradable_max_days"])
    days = ref.liquidity_bucket_days.get(p.liquidity_bucket)
    if days is None:
        raise KeyError(f"liquidity_buckets has no row for {p.liquidity_bucket!r}")
    return days <= max_days or (ref.is_maturing(p.instrument) and p.residual_days <= max_days)


# =============================================================================
# PARA 61 BUCKETS
# =============================================================================
def _matches(rule: dict, p: Position, ref: Reference) -> bool:
    test = rule["issuer_or_instrument_test"]

    if test == "ART_17_7":
        if not ref.qualifies_article_17_7(p.instrument, p.issuer_type):
            return False
    elif test == "CASH":
        if ref.asset_category(p.instrument) != "CASH":
            return False
    elif test == "REVERSE_REPO":
        if ref.asset_category(p.instrument) != "REVERSE_REPO":
            return False
    elif test == "SECURITISATION_ABCP":
        if ref.asset_category(p.instrument) != "SECURITISATION_ABCP":
            return False
    elif test == "MMI_OR_MMF":
        # Art 9(1) separates (a) money market instruments from (b) securitisations and
        # ABCPs, and para 61 gives each its own line - line 2 at CQS 1-2, line 3 at CQS 1.
        # Letting a securitisation in through line 2 would make line 3's stricter CQS test
        # unreachable, so this line admits only categories (a) and (g).
        if ref.asset_category(p.instrument) not in ("MMI", "MMF_UNITS"):
            return False
        if not is_eligible_mmi(p, ref):
            return False
    elif test != "ANY":
        raise ReferenceError(f"wla_bucket_rules: unknown test {test!r}")
    # test == "ANY" places no instrument condition; the maturity limit below does the work.

    cqs_required = rule["cqs_required"]
    if cqs_required:
        allowed = {int(x) for x in str(cqs_required).split(",")}
        if p.cqs is None or p.cqs not in allowed:
            return False

    max_settle = rule["max_settlement_days"]
    if max_settle is not None:
        # A reverse repo or cash balance qualifies on its notice; everything else on settlement.
        days = (p.termination_notice_days if _terminable(p, ref)
                and p.termination_notice_days is not None else p.settlement_days)
        if days > max_settle:
            return False

    # Both maturity limits apply only to assets that MATURE - shares in another MMF are
    # redeemable, not maturing. The 190-day limit is calendar days; the weekly window is
    # working days.
    max_resid = rule["max_residual_maturity_days"]
    if max_resid is not None:
        if not ref.is_maturing(p.instrument) or p.residual_days > max_resid:
            return False
    max_working = rule["max_maturity_working_days"]
    if max_working is not None:
        if not ref.is_maturing(p.instrument) or p.maturity_working_days > max_working:
            return False

    return True


def wla_bucket(p: Position, ref: Reference) -> BucketResult:
    # "Able to be redeemed and settled within N working days" reads either as convertible
    # to cash (SETTLEMENT) or as maturing inside the window (MATURITY). See the
    # wla_bucket2_mmi_basis note in engine_settings - the choice moves RST-02/03 and MST-02.
    strict = ref.settings.get("wla_bucket2_mmi_basis") == "MATURITY"
    for bucket in (1, 2):
        for rule in ref.wla_rules:
            if int(rule["bucket"]) != bucket:
                continue
            if not _matches(rule, p, ref):
                continue
            if (strict and bucket == 2 and rule["max_settlement_days"] is not None
                    and p.maturity_working_days > rule["max_settlement_days"]):
                continue
            return BucketResult(f"Bucket {bucket}", float(rule["weight"]),
                                f"para 61 bucket {bucket}, Art {rule['article']}")
    return BucketResult("None", 0.0, "meets no para 61 criterion")


def weekly_liquid_assets(fund: Fund, ref: Reference,
                         values: dict[str, float] | None = None) -> dict:
    """Bucket 1 and bucket 2 at the weights para 61 gives them.

    `values` supplies stressed market values keyed by security id. Bucket ELIGIBILITY does
    not change under a shock - an instrument does not stop being Article 17(7) paper because
    its price moved - so only the amounts are substituted. para 63 needs this: the macro
    scenario measures weekly liquid assets AFTER the market shock.
    """
    b1 = b2 = 0.0
    for p in fund.positions:
        mv = values.get(p.security_id, p.market_value) if values else p.market_value
        r = wla_bucket(p, ref)
        if r.bucket == "Bucket 1":
            b1 += mv
        elif r.bucket == "Bucket 2":
            b2 += mv
    return {"bucket_1": b1, "bucket_2": b2,
            "total": b1 * ref.bucket_weight(1) + b2 * ref.bucket_weight(2)}


# =============================================================================
# PORTFOLIO LIMITS
# =============================================================================
@dataclass(frozen=True)
class ShareLimit:
    """One cap on a group of holdings, as a share of total assets."""
    article: str
    name: str
    cap: float
    members: tuple[int, ...]     # indices into the positions the list was built from


def body_limit_label(limits: dict) -> str:
    """Article 17(1)(a) at 5%, or the Article 17(2) VNAV derogation at 10%."""
    same = abs(limits["art17_diversification_cap"] - limits["art17_single_body_base_cap"]) < 1e-12
    return "Art 17(1)(a)" if same else "Art 17(2)"


def share_limits(positions: list[Position], ref: Reference, limits: dict) -> list[ShareLimit]:
    """Every limit that caps a group of holdings as a share of total assets.

    One list serves both the compliance test and the RST-01 optimiser, so the two cannot
    disagree about which holdings a limit covers. A body is the issuer_name; each
    security_id is one issue.
    """
    body = body_limit_label(limits)
    caps = {
        "Art 16(2)": limits["art16_single_mmf_cap"],
        "Art 16(3)": limits["art16_aggregate_mmf_cap"],
        body: limits["art17_diversification_cap"],
        "Art 17(1)(b)": limits["art17_deposit_bank_cap"],
        "Art 17(3)": limits["art17_securitisation_cap"],
        "Art 17(3) non-STS": limits["art17_securitisation_non_sts_cap"],
        "Art 17(5)": limits["art17_repo_counterparty_cap"],
        "Art 17(6)": limits["art17_combined_body_cap"],
        "Art 17(7)(b)": limits["art17_7_max_issue_share"],
    }
    groups: dict[tuple[str, str], list[int]] = {}

    def add(article: str, name: str, i: int) -> None:
        groups.setdefault((article, name), []).append(i)

    for i, p in enumerate(positions):
        category = ref.asset_category(p.instrument)
        scope = ref.diversification_scope(p.instrument)
        if category == "MMF_UNITS":
            add("Art 16(2)", p.issuer, i)
            add("Art 16(3)", "units of other MMFs", i)
        elif category == "REVERSE_REPO":
            add("Art 17(5)", f"reverse repos with {p.issuer}", i)
        elif scope == "DEPOSIT":
            add("Art 17(1)(b)", f"deposits with {p.issuer}", i)
            add("Art 17(6)", f"{p.issuer} combined", i)
        elif scope == "MMI":
            if is_public_debt(p, ref):
                add("Art 17(7)(b)", f"issue {p.security_id}", i)
            else:
                add(body, p.issuer, i)
                add("Art 17(6)", f"{p.issuer} combined", i)
            if category == "SECURITISATION_ABCP":
                add("Art 17(3)", "securitisations and ABCPs", i)
                if not p.sts:
                    add("Art 17(3) non-STS", "securitisations and ABCPs", i)
    return [ShareLimit(a, n, caps[a], tuple(ix)) for (a, n), ix in groups.items()]


def public_debt_issues(positions: list[Position], ref: Reference) -> dict[str, tuple[int, ...]]:
    """Article 17(7) issuer -> indices of the issues held from it."""
    out: dict[str, list[int]] = {}
    for i, p in enumerate(positions):
        if is_public_debt(p, ref):
            out.setdefault(p.issuer, []).append(i)
    return {k: tuple(v) for k, v in out.items()}


def portfolio_ratios(fund: Fund, ref: Reference, positions: list[Position] | None = None) -> dict:
    """Every portfolio limit, on the whole fund or on a residual subset.

    Passing a subset is what lets the RST-01 solver ask "would the fund still comply after
    selling this much?" without duplicating the rules.
    """
    if ref.settings.get("art17_3_basis") != "STS_FLAG":
        raise ReferenceError(f"art17_3_basis {ref.settings.get('art17_3_basis')!r} is not supported")
    if ref.settings.get("public_debt_diversification") != "ART_17_7_CONDITIONS":
        raise ReferenceError("public_debt_diversification "
                             f"{ref.settings.get('public_debt_diversification')!r} is not supported")

    pos = fund.positions if positions is None else positions
    limits = ref.limits(fund.fund_type, fund.nav_type)
    total = sum(p.market_value for p in pos)
    if total <= 0:
        return {"total": 0.0, "compliant": False, "breaches": ["no assets remain"]}
    tol = 1e-9

    def w(p):
        return p.market_value / total

    wam = sum(w(p) * p.wam_days for p in pos)
    wal = sum(w(p) * p.wal_days for p in pos)

    daily_pct = sum(w(p) for p in pos if is_daily_liquid(p, ref, limits))
    weekly_base = sum(w(p) for p in pos if is_weekly_liquid(p, ref, limits))
    cap = inclusion_cap(limits)
    eligible = sum(w(p) for p in pos if counts_toward_inclusion(p, ref, limits))
    weekly_pct = weekly_base + (min(eligible, cap) if cap is not None else 0.0)

    breaches = []
    if wam > limits["wam_cap_days"]:
        breaches.append(f"WAM {wam:.1f}d > {limits['wam_cap_days']}d")
    if wal > limits["wal_cap_days"]:
        breaches.append(f"WAL {wal:.1f}d > {limits['wal_cap_days']}d")
    if daily_pct < limits["daily_liquid_min"] - tol:
        breaches.append(f"daily {daily_pct:.2%} < {pct(limits['daily_liquid_min'])}")
    if weekly_pct < limits["weekly_liquid_min"] - tol:
        breaches.append(f"weekly {weekly_pct:.2%} < {pct(limits['weekly_liquid_min'])}")

    # ---- Articles 16 and 17: caps on groups of holdings ------------------------------
    worst: dict[str, tuple[str, float, float]] = {}      # article -> (name, share, cap)
    body = body_limit_label(limits)
    base = limits["art17_single_body_base_cap"]
    aggregate, at_base = 0.0, 0
    for g in share_limits(pos, ref, limits):
        share = sum(pos[i].market_value for i in g.members) / total
        if g.article not in worst or share > worst[g.article][1]:
            worst[g.article] = (g.name, share, g.cap)
        if share > g.cap + tol:
            breaches.append(f"{g.article} {g.name} {share:.2%} of fund > {pct(g.cap)}")
        if g.article == body and share > base + tol:
            aggregate += share
        elif g.article == body and share >= base - 1e-6:
            at_base += 1          # exactly at the threshold: outside the aggregate, but only just
    agg_cap = limits["art17_diversification_aggregate"]
    if agg_cap is not None and aggregate > agg_cap + tol:
        breaches.append(f"Art 17(2) bodies above {pct(base)} total {aggregate:.2%} > {pct(agg_cap)}")

    # Article 17(7): above 5%, a public-debt issuer needs six issues or more.
    public_debt = {}
    for issuer, ix in public_debt_issues(pos, ref).items():
        share = sum(pos[i].market_value for i in ix) / total
        public_debt[issuer] = (share, len(ix))
        if share > base + tol and len(ix) < limits["art17_7_min_issues"]:
            breaches.append(f"Art 17(7) {issuer} {share:.2%} of fund in {len(ix)} issues, "
                            f"needs {limits['art17_7_min_issues']} above {pct(base)}")

    # ---- Article 18 -----------------------------------------------------------------
    # CONCENTRATION - a different denominator entirely. Article 17 asks what share of THE
    # FUND sits with one issuer; Article 18 asks what share of THAT ISSUER's outstanding
    # money market instruments, securitisations and ABCPs the fund holds. Article 18(2)
    # exempts public debt. Selling reduces the holding while the issuer's outstanding is
    # unchanged, so this ratio only improves during an RST-01 liquidation.
    held: dict[str, float] = {}
    issue_size: dict[str, float] = {}
    for p_ in pos:
        if ref.diversification_scope(p_.instrument) != "MMI" or is_public_debt(p_, ref):
            continue
        if not p_.issuer_outstanding:
            continue
        held[p_.issuer] = held.get(p_.issuer, 0.0) + p_.market_value
        issue_size[p_.issuer] = p_.issuer_outstanding
    art18 = {k: held[k] / issue_size[k] for k in held if issue_size.get(k)}
    a18_issuer, a18_share = max(art18.items(), key=lambda kv: kv[1]) if art18 else ("-", 0.0)
    cap18 = limits.get("art18_concentration_cap")
    if cap18 is not None and a18_share > cap18 + tol:
        breaches.append(f"Art 18 {a18_issuer} {a18_share:.2%} of issue > {pct(cap18)}")

    top_issuer, top_share, _cap = worst.get(body, ("-", 0.0, limits["art17_diversification_cap"]))
    return {"total": total, "wam": wam, "wal": wal, "daily": daily_pct,
            "weekly": weekly_pct, "weekly_base": weekly_base, "inclusion_eligible": eligible,
            "top_issuer": top_issuer, "top_share": top_share, "art17_aggregate": aggregate,
            "art17_bodies_at_base": at_base,
            "share_limits": worst, "public_debt": public_debt,
            "art18_issuer": a18_issuer, "art18_share": a18_share,
            "breaches": breaches, "compliant": not breaches}
