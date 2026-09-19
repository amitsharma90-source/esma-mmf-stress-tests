"""Run the ESMA MMF stress test end to end.

    C:\\Users\\amits\\anaconda3\\python.exe V02\\engine\\run_stress_test.py

Reads esma_reference.xlsx, fund_static.xlsx and the position file; writes a portfolio
report (one row per fund and scenario) and a position report. Valuation date is a run
parameter, not a column in the data.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from esma_mmf import audit, mst_audit, optimise, report
from esma_mmf.classify import portfolio_ratios
from esma_mmf.loader import load, resolve_fund_static
from esma_mmf.measures import measure_fund
from esma_mmf.reference import Reference
from esma_mmf.scenarios import run

ENGINE = Path(__file__).resolve().parent
EXTRACTED = ENGINE.parent / "extracted"
OUTPUT = ENGINE.parent / "output"


def main() -> int:
    ap = argparse.ArgumentParser(description="ESMA MMF calibrated stress test")
    ap.add_argument("--valuation-date", default="2026-06-30")
    ap.add_argument("--reference", default=EXTRACTED / "esma_reference.xlsx")
    ap.add_argument("--fund-static", default=None)
    ap.add_argument("--positions", default=EXTRACTED / "ESMA_MMF_Stress_Input.xlsx")
    ap.add_argument("--out", default=OUTPUT)
    args = ap.parse_args()

    valuation: date = datetime.strptime(args.valuation_date, "%Y-%m-%d").date()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    ref = Reference(args.reference)
    fund_static = args.fund_static or resolve_fund_static(EXTRACTED)
    funds = load(args.positions, fund_static, ref, valuation)
    print(f"Valuation date {valuation}   |   reference {Path(args.reference).name}")
    print(f"Loaded {sum(len(f.positions) for f in funds)} positions across {len(funds)} funds\n")

    measures, results, solved = {}, {}, {}
    for f in funds:
        ratios = portfolio_ratios(f, ref)
        if not ratios["compliant"]:
            print(f"  ! {f.code} is not compliant at rest: {'; '.join(ratios['breaches'])}")
        if f.nav_override:
            drift = abs(f.nav_override - f.total_assets) / f.total_assets
            if drift > 0.005:
                print(f"  ! {f.code}: nav_override differs from total assets by {drift:.2%}")
        measures[f.code] = measure_fund(f, ref)
        # RST-01 is solved once and shared, so the report and the audit cannot differ.
        solved[f.code] = optimise.solve(f, ref)
        results[f.code] = run(f, ref, measures[f.code], solved[f.code])

    by_code = {f.code: f for f in funds}
    portfolio_path = out_dir / f"ESMA_MMF_Portfolio_Report_{valuation:%Y%m%d}.xlsx"
    positions_path = out_dir / f"ESMA_MMF_Position_Report_{valuation:%Y%m%d}.xlsx"
    report.write_portfolio(portfolio_path, results, by_code, valuation)
    report.write_positions(positions_path, funds, measures)
    # Purely additive: the audit re-uses the engine's own functions and records what they
    # return, so it can explain a result but never change one.
    audit_path = out_dir / f"ESMA_MMF_Scenario_Audit_{valuation:%Y%m%d}.xlsx"
    audit.write(audit_path, funds, ref, measures, solved)
    mst_audit_path = out_dir / f"ESMA_MMF_MST_Audit_{valuation:%Y%m%d}.xlsx"
    mst_audit.write(mst_audit_path, funds, ref, measures)

    codes = [f.code for f in funds]
    order = [s.code for s in results[codes[0]]]
    width = max(len(c) for c in codes) + 2
    print("  " + "scenario".ljust(10) + "".join(c.rjust(width + 8) for c in codes))
    for i, sc in enumerate(order):
        cells = ""
        for c in codes:
            s = results[c][i]
            if s.impact_on_nav is not None:
                cells += f"{s.impact_on_nav:>{width + 8}.2%}"
            else:
                cells += f"{s.first_bucket:>{width + 3}.0%}/{s.total_bucket:<4.0%}"
        print(f"  {sc:<10}{cells}")

    print()
    for c in codes:
        rst01 = next(s for s in results[c] if s.code == "RST-01")
        print(f"  RST-01 {c:<13}{rst01.impact_on_nav:>7.2%}   {rst01.note}")

    problems = (report.check(portfolio_path) + report.check(positions_path)
                + report.check(audit_path) + report.check(mst_audit_path))
    print(f"\nWrote {portfolio_path.name} and {positions_path.name} to {out_dir}")
    print(f"Audit workbooks: {audit_path.name}, {mst_audit_path.name}")
    if problems:
        print("WORKBOOK INTEGRITY FAILURES:")
        for p in problems:
            print(f"  x {p}")
        return 1
    print("Both workbooks pass the Excel integrity checks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
