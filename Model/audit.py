"""Scenario audit workbook - the working behind the redemption scenarios.

Three sheets, one per natural grain:

    rst01_summary   one row per fund      inputs, RST-01/02/03 results, how RST-01 was reached
    rst01_ladder    one row per level     every portfolio limit along the RST-01 sale path
    positions       one row per holding   what RST-01 sells of it, and its WLA bucket

Every figure comes from the engine's own functions - optimise.solve for the sale,
portfolio_ratios for every limit along the way, wla_bucket for the buckets. Nothing here
can change an engine result.
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from . import optimise
from .classify import is_weekly_tradable, portfolio_ratios, weekly_liquid_assets, wla_bucket
from .loader import Fund
from .measures import PositionMeasures, blended_redemption
from .reference import Reference

NAVY = "FF0E2C4B"
AMBER = "FFFFF2CC"
GREEN = "FFE7F0EC"
WHITE_BOLD = Font(color="FFFFFFFF", bold=True, size=10)

LADDER = [i / 20.0 for i in range(21)]
# Presentation only: the ladder adds one row this far past an optimal RST-01, along the same
# sale, so the limit that stops the sale is seen breaking rather than merely at its cap.
BEYOND_STEP = 0.01


def _sheet(wb, name, columns, note):
    """columns: sequence of (header, width, number_format or None)."""
    ws = wb.create_sheet(name)
    ws.append([note])
    ws["A1"].font = Font(italic=True, size=9, color="FF555555")
    ws.append([])
    ws.append([c for c, _w, _f in columns])
    for i in range(1, len(columns) + 1):
        c = ws.cell(row=3, column=i)
        c.font = WHITE_BOLD
        c.fill = PatternFill("solid", fgColor=NAVY)
        c.alignment = Alignment(horizontal="left", vertical="center")
    for i, (_c, w, _f) in enumerate(columns, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "D4"
    return ws


def _apply_formats(ws, columns):
    """Formats go only on cells holding a value - an empty typed cell voids the file."""
    for row in range(4, ws.max_row + 1):
        for i, (_c, _w, fmt) in enumerate(columns, start=1):
            if fmt and ws.cell(row=row, column=i).value is not None:
                ws.cell(row=row, column=i).number_format = fmt


def _shade(ws, ncols, col, value, colour):
    """Shade a whole row.

    Styling an EMPTY cell materialises it, and openpyxl defaults a fresh cell to the
    numeric type - producing <c t="n"/> with no value, which Excel rejects outright.
    Empty cells therefore get their type forced to string before being filled.
    """
    fill = PatternFill("solid", fgColor=colour)
    for row in range(4, ws.max_row + 1):
        if ws.cell(row=row, column=col).value == value:
            for c in range(1, ncols + 1):
                cell = ws.cell(row=row, column=c)
                cell.fill = fill
                if cell.value is None:
                    cell.data_type = "s"


# (header, width, format, share_limits key) for the Article 16/17 columns of the ladder.
_GROUP_COLUMNS = [
    ("art16_top_mmf_pct", "art16_single_cap", "Art 16(2)"),
    ("art16_mmf_total_pct", "art16_aggregate_cap", "Art 16(3)"),
    ("art17_top_deposit_bank_pct", "art17_deposit_cap", "Art 17(1)(b)"),
    ("art17_securitisation_pct", "art17_securitisation_cap", "Art 17(3)"),
    ("art17_non_sts_pct", "art17_non_sts_cap", "Art 17(3) non-STS"),
    ("art17_top_repo_counterparty_pct", "art17_repo_cap", "Art 17(5)"),
    ("art17_top_combined_body_pct", "art17_combined_cap", "Art 17(6)"),
    ("art17_7_top_issue_pct", "art17_7_issue_cap", "Art 17(7)(b)"),
]
_GROUP_CAPS = {
    "Art 16(2)": "art16_single_mmf_cap", "Art 16(3)": "art16_aggregate_mmf_cap",
    "Art 17(1)(b)": "art17_deposit_bank_cap", "Art 17(3)": "art17_securitisation_cap",
    "Art 17(3) non-STS": "art17_securitisation_non_sts_cap",
    "Art 17(5)": "art17_repo_counterparty_cap", "Art 17(6)": "art17_combined_body_cap",
    "Art 17(7)(b)": "art17_7_max_issue_share",
}


def write(path: Path, funds: list[Fund], ref: Reference,
          measures: dict[str, dict[str, PositionMeasures]],
          solved: dict[str, optimise.RST01Result] | None = None) -> None:
    wb = Workbook()
    wb.remove(wb.active)
    solved = solved or {f.code: optimise.solve(f, ref) for f in funds}
    w1 = ref.bucket_weight(1)

    # ---- 1. one row per fund ------------------------------------------------
    cols = [
        ("fund", 14, None), ("NAV", 18, "#,##0.00"), ("total_assets", 18, "#,##0.00"),
        ("institutional", 14, "0%"), ("retail", 10, "0%"),
        ("blended_redemption", 19, "0.0%"), ("macro_redemption", 18, "0.0%"),
        ("top2_investors", 16, "0.00%"),
        ("bucket_1", 18, "#,##0.00"), ("bucket_2", 18, "#,##0.00"),
        ("weighted_WLA", 18, "#,##0.00"), ("WLA_pct_of_NAV", 16, "0.0%"),
        ("RST02_outflow", 18, "#,##0.00"), ("RST02_first", 13, "0.00%"),
        ("RST02_total", 13, "0.00%"),
        ("RST03_outflow", 18, "#,##0.00"), ("RST03_first", 13, "0.00%"),
        ("RST03_total", 13, "0.00%"),
        ("RST01", 11, "0.00%"), ("tradable_ceiling", 16, "0.00%"),
        ("optimiser_end_state", 20, "0.00%"), ("path_points_tested", 19, None),
        ("path_compliant_to", 18, "0.00%"), ("RST01_status", 26, None),
        ("RST01_binding_constraint", 80, None),
    ]
    ws = _sheet(wb, "rst01_summary", cols,
                "One row per fund. RST01 is the reported figure (para 60): the largest share of "
                "total assets that can be sold from the weekly tradable book with every portfolio "
                "limit met at every step. optimiser_end_state is where the optimiser ends the sale; "
                "the holdings are then moved toward it in even steps and the fund re-tested against "
                "every limit at each of path_points_tested points. path_compliant_to is how far along "
                "that path the fund stayed compliant, so RST01 = optimiser_end_state x "
                "path_compliant_to. tradable_ceiling is everything that could be sold inside a week.")
    for f in funds:
        r = solved[f.code]
        w = weekly_liquid_assets(f, ref)
        red, macro = blended_redemption(f, ref), blended_redemption(f, ref, macro=True)
        out2, out3 = red * f.nav, f.top2_investor_share * f.nav
        ws.append([
            f.code, f.nav, f.total_assets, f.institutional_share, f.retail_share,
            red, macro, f.top2_investor_share,
            w["bucket_1"], w["bucket_2"], w["total"], w["total"] / f.nav,
            out2, w["bucket_1"] * w1 / out2, w["total"] / out2,
            out3, w["bucket_1"] * w1 / out3, w["total"] / out3,
            r.fraction, r.ceiling, r.solver_fraction, r.path_steps + 1, r.path_reached,
            r.status, r.note,
        ])
    _apply_formats(ws, cols)

    # ---- 2. one row per liquidation level -----------------------------------
    cols = [
        ("fund", 14, None), ("liquidated_pct", 15, "0.00%"),
        ("residual_assets", 18, "#,##0"),
        ("WAM", 9, "0.0"), ("WAM_cap", 10, None), ("WAL", 9, "0.0"), ("WAL_cap", 10, None),
        ("daily", 9, "0.00%"), ("daily_min", 10, "0.00%"),
        ("weekly", 9, "0.00%"), ("weekly_min", 11, "0.00%"),
        ("art17_top_issuer_pct_of_fund_Diversification", 42, "0.00%"), ("art17_cap", 11, "0.00%"),
        ("art17_aggregate_of_bodies_over_base", 34, "0.00%"), ("art17_aggregate_cap", 20, "0.00%"),
        ("art17_bodies_at_base", 21, None),
        ("art17_7_top_issuer_pct", 22, "0.00%"), ("art17_7_top_issuer_issues", 25, None),
        ("art17_7_single_body_cap", 23, "0.00%"), ("art17_7_min_issues", 18, None),
    ]
    for share_col, cap_col, _key in _GROUP_COLUMNS:
        cols += [(share_col, max(14, len(share_col) + 2), "0.00%"), (cap_col, max(12, len(cap_col) + 2), "0.00%")]
    cols += [
        ("art18_top_issuer_pct_of_issue_Concentration", 42, "0.00%"), ("art18_cap", 11, "0.00%"),
        ("compliant", 11, None), ("marker", 20, None), ("first_breach", 60, None),
    ]
    ws = _sheet(wb, "rst01_ladder", cols,
                "para 60. Each row sells that share of total assets along the RST-01 sale path - "
                "every weekly tradable holding moving in step toward the optimiser's end state - and "
                "re-tests the REMAINING portfolio against every limit. The row marked RST-01 is the "
                "answer; the row marked 'beyond RST-01' continues the same sale one percentage point "
                "further and shows the limit that stops it. art17_7_top_issuer is the largest "
                "public-debt issuer, which needs art17_7_min_issues issues once above "
                "art17_7_single_body_cap. art17_top_issuer is the share of THE FUND one body takes; art18 is the share "
                "of AN ISSUER the fund holds, which can only fall as the fund sells. Article 16 caps "
                "units of other MMFs, which sit outside Article 17.")
    compliant_col = [c for c, _w, _f in cols].index("compliant") + 1
    marker_col = compliant_col + 1
    for f in funds:
        limits = ref.limits(f.fund_type, f.nav_type)
        r = solved[f.code]
        end = r.solver_fraction
        points = {x for x in LADDER if x <= end + 1e-9} | {r.fraction, end}
        breach_at = None
        if r.status == "cut back on the sale path":
            breach_at = min(end, end * (r.path_reached + 1.0 / r.path_steps))
            points.add(breach_at)
        caps = [limits[_GROUP_CAPS[key]] for _s, _c, key in _GROUP_COLUMNS]
        pd_caps = [limits["art17_single_body_base_cap"], limits["art17_7_min_issues"]]

        rungs = []
        for frac in sorted(points):
            marker = ("RST-01" if abs(frac - r.fraction) < 1e-12 else
                      "first breach" if breach_at is not None and abs(frac - breach_at) < 1e-12 else
                      "optimiser end state" if abs(frac - end) < 1e-12 else "")
            if marker == "RST-01" and abs(r.fraction - r.ceiling) < 1e-9:
                marker = "RST-01 = tradable ceiling"
            rungs.append((frac, frac / end if end > 0 else 0.0, marker))
        if r.status == "optimal" and end > 0:
            rungs.append((None, min(r.ceiling, r.fraction + BEYOND_STEP) / end, "beyond RST-01"))

        for frac, t, marker in rungs:
            residual = optimise.residual_after(f, r.sold, t)
            if frac is None:      # past the end state some holdings run out, so measure it
                frac = 1.0 - sum(p.market_value for p in residual) / f.total_assets
            if not residual:
                row = [f.code, frac, 0.0, None, limits["wam_cap_days"], None, limits["wal_cap_days"],
                       None, limits["daily_liquid_min"], None, limits["weekly_liquid_min"],
                       None, limits["art17_diversification_cap"],
                       None, limits["art17_diversification_aggregate"], None, None, None] + pd_caps
                for cap in caps:
                    row += [None, cap]
                row += [None, limits.get("art18_concentration_cap"), "TRUE", marker,
                        "sold outright, no portfolio remains"]
                ws.append(row)
                continue
            q = portfolio_ratios(f, ref, residual)
            groups = q["share_limits"]
            pd_top = max(q["public_debt"].values(), default=(0.0, None))
            row = [f.code, frac, q["total"], q["wam"], limits["wam_cap_days"],
                   q["wal"], limits["wal_cap_days"], q["daily"], limits["daily_liquid_min"],
                   q["weekly"], limits["weekly_liquid_min"],
                   q["top_share"], limits["art17_diversification_cap"],
                   q["art17_aggregate"], limits["art17_diversification_aggregate"],
                   q["art17_bodies_at_base"], pd_top[0], pd_top[1]] + pd_caps
            for (_s, _c, key), cap in zip(_GROUP_COLUMNS, caps):
                row += [groups[key][1] if key in groups else 0.0, cap]
            row += [q["art18_share"], limits.get("art18_concentration_cap"),
                    "TRUE" if q["compliant"] else "FALSE", marker,
                    q["breaches"][0] if q["breaches"] else ""]
            ws.append(row)
    _apply_formats(ws, cols)
    _shade(ws, len(cols), compliant_col, "FALSE", AMBER)
    _shade(ws, len(cols), marker_col, "RST-01", GREEN)
    _shade(ws, len(cols), marker_col, "RST-01 = tradable ceiling", GREEN)

    # ---- 3. one row per position -------------------------------------------
    cols = [
        ("fund", 14, None), ("security_id", 13, None), ("instrument", 24, None),
        ("issuer", 38, None), ("liquidity_bucket", 18, None), ("liquidation_days", 17, None),
        ("residual_days", 14, None), ("market_value", 18, "#,##0.00"),
        ("weekly_tradable", 16, None), ("sold_at_RST01", 18, "#,##0.00"),
        ("sold_pct_of_holding", 20, "0.00%"), ("retained_at_RST01", 19, "#,##0.00"),
        ("status_at_RST01", 17, None), ("tradable_reason", 56, None),
        ("cqs", 7, None), ("wla_bucket", 12, None), ("wla_weight", 12, "0%"),
        ("wla_qualifying_rule", 34, None), ("wla_weighted_contribution", 26, "#,##0.00"),
    ]
    ws = _sheet(wb, "positions", cols,
                "Every holding once: how much of it RST-01 sells on the left, the weekly liquid asset "
                "bucket it falls into on the right. The sale is not a selling order - the optimiser "
                "decides how much of each weekly tradable holding goes, so several holdings can be "
                "PARTIAL at once. A position can be sold under RST-01 and still count toward RST-02's "
                "bucket - the two scenarios ask different questions of the same holding.")
    max_days = int(ref.settings["weekly_tradable_max_days"])
    for f in funds:
        sold = solved[f.code].sold_at_rst01()
        for p in f.positions:
            liq = ref.liquidity_bucket_days.get(p.liquidity_bucket)
            tradable = is_weekly_tradable(p, ref)
            if tradable:
                amount = sold.get(p.security_id, 0.0)
                status = ("SOLD" if amount >= p.market_value * (1 - 1e-9) else
                          "PARTIAL" if amount > p.market_value * 1e-9 else "RETAINED")
                why = (f"liquidates in {p.liquidity_bucket}, inside the {max_days}-day window"
                       if liq is not None and liq <= max_days
                       else f"matures in {p.residual_days}d, inside one week")
                sold_v, pct_v, kept_v = amount, amount / p.market_value, p.market_value - amount
            else:
                status, sold_v, pct_v, kept_v = "NOT TRADABLE", None, None, p.market_value
                why = (f"liquidates in {p.liquidity_bucket} and matures in {p.residual_days}d - "
                       f"neither inside the {max_days}-day window")
            b = wla_bucket(p, ref)
            ws.append([f.code, p.security_id, p.instrument, p.issuer, p.liquidity_bucket, liq,
                       p.residual_days, p.market_value, "YES" if tradable else "NO",
                       sold_v, pct_v, kept_v, status, why, p.cqs, b.bucket, b.weight, b.reason,
                       p.market_value * b.weight])
    _apply_formats(ws, cols)
    _shade(ws, len(cols), 13, "NOT TRADABLE", AMBER)
    _shade(ws, len(cols), 13, "PARTIAL", GREEN)

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
