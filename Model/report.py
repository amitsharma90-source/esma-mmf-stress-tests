"""Write the two output workbooks.

Both carry the Excel-integrity guards learned the hard way: number formats go only on cells
that hold a value, no column-level styles, and no cell text beginning with "=". Any of the
three makes Excel refuse the file, and openpyxl reads all three back without complaint.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .loader import Fund
from .measures import PositionMeasures
from .scenarios import ScenarioResult

NAVY = "FF0E2C4B"
WHITE_BOLD = Font(color="FFFFFFFF", bold=True, size=10)

PORTFOLIO_COLUMNS = [
    ("fund", 34), ("fund_code", 14), ("risk_factor", 22), ("scenario_code", 15),
    ("position_date", 15), ("net_asset_value", 20), ("impact_on_nav", 15),
    ("first_bucket_outflow", 21), ("total_bucket_outflow", 21), ("basis", 62),
]

POSITION_COLUMNS = [
    ("fund", 30), ("fund_code", 13), ("security_id", 13), ("security_description", 44),
    ("issuer", 40), ("instrument_type", 24), ("maturity_date", 14), ("rate_type", 12),
    ("residual_days", 14), ("rate_duration", 14), ("spread_duration", 16), ("yield", 11),
    ("market_value", 18), ("cqs", 7), ("wla_bucket", 12), ("wla_weight", 12),
    ("LST01Shock_LDF", 16), ("LST01Shock_MIF", 16), ("LST01Shock", 14),
    ("LST01Shock_MV", 16), ("liquidityLST01", 17), ("liquidityLST01_MST", 20),
    ("CST01Shock", 13), ("CST02LGD", 11), ("IST01Shock", 13),
    ("creditCST01", 16), ("creditCST02", 18), ("interestRatesIST01", 20),
    ("spreadSST01", 16), ("FXrateFST01", 13), ("FXrateFST02", 13),
    ("weeklyTradableAmount", 21),
]

RATE_FMT, MONEY_FMT, PCT_FMT, SHOCK_FMT = "0.000000000", "#,##0.00", "0.00%", "0.00E+00"
POSITION_FORMATS = {
    "rate_duration": RATE_FMT, "spread_duration": RATE_FMT, "yield": RATE_FMT,
    "market_value": MONEY_FMT, "wla_weight": "0%", "LST01Shock_LDF": "0.0000",
    "LST01Shock_MIF": SHOCK_FMT, "LST01Shock": RATE_FMT,
    "LST01Shock_MV": MONEY_FMT, "liquidityLST01": MONEY_FMT, "liquidityLST01_MST": MONEY_FMT,
    "CST01Shock": "0.0000", "CST02LGD": "0.00", "IST01Shock": "0.0000",
    "creditCST01": MONEY_FMT, "creditCST02": MONEY_FMT, "interestRatesIST01": MONEY_FMT,
    "spreadSST01": MONEY_FMT, "FXrateFST01": "0.00", "FXrateFST02": "0.00",
    "weeklyTradableAmount": MONEY_FMT,
}


def _header(ws, columns, row=1):
    ws.append([c for c, _w in columns])
    for i in range(1, len(columns) + 1):
        cell = ws.cell(row=row, column=i)
        cell.font = WHITE_BOLD
        cell.fill = PatternFill("solid", fgColor=NAVY)
        cell.alignment = Alignment(horizontal="left", vertical="center")
    for i, (_c, w) in enumerate(columns, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _safe(v):
    """Prose beginning with '=' would be written as a formula and stripped by Excel."""
    return f"'{v}" if isinstance(v, str) and v.startswith("=") else v


def write_portfolio(path: Path, results: dict[str, list[ScenarioResult]],
                    funds: dict[str, Fund], position_date) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "portfolio"
    _header(ws, PORTFOLIO_COLUMNS)
    for code, scenarios in results.items():
        f = funds[code]
        for s in scenarios:
            ws.append([f.name, code, s.risk_factor, s.code, position_date, f.nav,
                       s.impact_on_nav, s.first_bucket, s.total_bucket, _safe(s.note)])
    for row in range(2, ws.max_row + 1):
        for col, fmt in ((5, "DD/MM/YYYY"), (6, MONEY_FMT), (7, PCT_FMT),
                         (8, PCT_FMT), (9, PCT_FMT)):
            if ws.cell(row=row, column=col).value is not None:
                ws.cell(row=row, column=col).number_format = fmt
    ws.freeze_panes = "A2"
    wb.save(path)


def write_positions(path: Path, funds: list[Fund],
                    measures: dict[str, dict[str, PositionMeasures]]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "positions"
    _header(ws, POSITION_COLUMNS)
    for f in funds:
        for p in f.positions:
            m = measures[f.code][p.security_id]
            ws.append([
                f.name, f.code, p.security_id, p.description, p.issuer, p.instrument,
                p.maturity_date, p.rate_type, p.residual_days, p.rate_duration,
                p.spread_duration, p.yield_, p.market_value, p.cqs, m.wla_bucket,
                m.wla_weight, m.ldf, m.mif, m.lst01_shock, m.lst01_shock_mv,
                m.liquidity_lst01, m.liquidity_lst01_mst, m.cst01_shock, m.cst02_lgd,
                m.ist01_shock, m.credit_cst01, m.credit_cst02, m.interest_rates_ist01,
                m.spread_sst01, m.fx_fst01, m.fx_fst02, m.weekly_tradable,
            ])
    index = {c: i for i, (c, _w) in enumerate(POSITION_COLUMNS, start=1)}
    for row in range(2, ws.max_row + 1):
        if ws.cell(row=row, column=index["maturity_date"]).value is not None:
            ws.cell(row=row, column=index["maturity_date"]).number_format = "DD/MM/YYYY"
        for name, fmt in POSITION_FORMATS.items():
            cell = ws.cell(row=row, column=index[name])
            if cell.value is not None:
                cell.number_format = fmt
    ws.freeze_panes = "D2"
    wb.save(path)


_BAD = [
    (re.compile(r'<c [^>]*t="n"[^>]*>\s*</c>|<c [^>]*t="n"\s*/>'), "numeric cells with no value"),
    (re.compile(r'<col (?![^>]*customFormat="1")[^>]*style="\d+"[^>]*/?>'), "styled <col> elements"),
    (re.compile(r"<f>"), "formula cells"),
]


def check(path: Path) -> list[str]:
    problems = []
    with zipfile.ZipFile(path) as z:
        for entry in z.namelist():
            if not entry.startswith("xl/worksheets/"):
                continue
            xml = z.read(entry).decode("utf-8")
            for rx, what in _BAD:
                hits = rx.findall(xml)
                if hits:
                    problems.append(f"{path.name}::{entry}: {len(hits)} {what}")
    return problems
