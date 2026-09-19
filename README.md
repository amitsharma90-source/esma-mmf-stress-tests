# ESMA MMF stress tests

A Python engine that computes the twelve ESMA calibrated stress tests for money market funds
required by Regulation (EU) 2017/1131, Article 28.

Every shock, threshold and routing rule comes from one reference workbook holding the ESMA
calibration tables and the Article limits. The engine carries no constants of its own: if a
number is not in that workbook, it cannot appear in a result.

It reads a position file and writes a portfolio report, a position report and two audit
workbooks that show the working behind every figure.

> **The fund data is illustrative.** Issuer names are real market names, chosen so ratings
> and countries match genuine rows in the ESMA tables. Every exposure, identifier and price
> is invented. The four funds are synthetic and reconcile to nothing.

## What it computes

| Risk factor | Tests | Reported as |
|---|---|---|
| Liquidity (para 50) | LST-01 | loss, % of NAV |
| Credit (paras 51-53) | CST-01, CST-02 | loss, % of NAV |
| Interest rate and FX (paras 54-57) | IST-01, FST-01, FST-02, SST-01 | loss, % of NAV |
| Level of redemption (paras 59-62) | RST-01 | share of assets sellable in a week |
| | RST-02, RST-03 | coverage: liquid assets ÷ outflow |
| Macro (paras 63-65) | MST-01 | loss, % of NAV |
| | MST-02 | coverage, post-shock |

A loss reads positive and a gain negative, following para 58. A coverage ratio above 100%
means weekly liquid assets cover the outflow being tested.

## What it reads

Three workbooks in `V02/extracted/`:

| File | Supplies |
|---|---|
| `esma_reference.xlsx` | The ESMA calibration in 27 sheets: Tables 1-14, the para 61 bucket rules, the Article 16/17/18/24/25 limits, the tenor maps and credit quality steps. |
| Fund static | Fund type and NAV type, which select the Article 24 or Article 25 limits; the professional and retail split that sets the redemption rate; the two largest investors' share used by RST-03. |
| `ESMA_MMF_Stress_Input.xlsx` | One row per holding, 27 columns. |

## Layout

```
V02/extracted/   the three inputs above
V02/engine/      esma_mmf/ - nine modules - plus run_stress_test.py, the entry point
V02/engine/tests/  the rule and known-answer suite
V02/output/      the four result workbooks
```

| Module | What it does |
|---|---|
| `reference.py` | Answers every calibration question from the reference workbook. The only module that learns a number. |
| `loader.py` | Reads the positions and fund static, derives residual and working days, durations, CQS. |
| `measures.py` | The nine position-level measures. A measure out of scope returns nothing, never zero. |
| `classify.py` | Para 61 liquidity buckets, and the single judge of compliance: Articles 16, 17, 18, 24 and 25. |
| `scenarios.py` | The twelve portfolio scenarios. |
| `optimise.py` | Solves RST-01: the largest weekly sale that keeps every limit, re-tested along the way. |
| `report.py` | Writes the portfolio and position workbooks, and checks each file will open in Excel. |
| `audit.py`, `mst_audit.py` | The working behind the redemption and macro scenarios. They record, never change. |

## Running it

Developed on Python 3.12.

```
pip install openpyxl numpy scipy
```

`scipy` is needed by the RST-01 solver. Then:

```
python V02/engine/run_stress_test.py
```

On Windows, `V02\engine\run.cmd` does the same. Note that `python` on the PATH is often the
Microsoft Store stub, which exits without running anything; use a real interpreter path if so.

The entry point takes `--valuation-date`, `--reference`, `--fund-static`, `--positions` and
`--out`. The valuation date is a run parameter rather than a column in the data, so the same
holdings can be re-run at another date:

```
python V02/engine/run_stress_test.py --valuation-date 2026-09-30 --positions my_holdings.xlsx
```

It prints the twelve results for each fund, names the limits that bound RST-01, and exits
non-zero if any workbook it wrote would fail to open.

To run the checks:

```
python V02/engine/tests/test_engine.py
```

## What comes out

| File | Grain | Holds |
|---|---|---|
| `ESMA_MMF_Portfolio_Report` | fund × scenario | the reportable result, with a note on how each was reached |
| `ESMA_MMF_Position_Report` | holding | the nine measures and the inputs behind them |
| `ESMA_MMF_Scenario_Audit` | 3 sheets | RST-01: how it was reached, every limit along the sale, and how much of each holding is sold |
| `ESMA_MMF_MST_Audit` | 2 sheets | the macro shock leg by leg, and liquid assets before and after it |

The audit workbooks explain results and can never change them: they re-use the engine's own
functions and record what those return.

## Using your own holdings

Point `--positions` at a file with the same 27 columns. Most are self-explanatory; these five
decide results and are easy to get wrong:

| Column | What it must mean |
|---|---|
| `liquidity_bucket` | The manager's judgement under para 60: the shortest period in which the position could be liquidated at or near carrying value. It drives RST-01. **Not** a settlement convention. |
| `settlement_days` | Working days to settle a sale or redemption. It drives the para 61 buckets, a different question from the one above. |
| `security_currency_code` | The currency the holding is denominated in; it drives the FX tests and the rate table. `portfolio_currency_code` is the fund's own and must match fund static. |
| `sts` | TRUE on securitisations and ABCPs meeting the STS criteria. Blank counts as non-STS, which is the stricter Article 17(3) cap. |
| `issuer_outstanding_amount` | The issuer's total outstanding money market paper, needed for the Article 18 concentration test. |

The run reports any fund that is already outside its limits before stress, since RST-01 has
no headroom to solve into from there.

## Where the judgement calls live

The regulation leaves a handful of choices open. All of them are named settings on the
`engine_settings` sheet of the reference workbook, each with its paragraph citation, and are
changed there rather than in code:

| Setting | Value |
|---|---|
| `impact_convention` | `LOSS_OVER_NAV` |
| `price_impact_unit` | `AS_STATED` |
| `sst_sensitivity_basis` | `SPREAD_DURATION` |
| `wla_bucket2_mmi_basis` | `SETTLEMENT` |
| `weekly_tradable_basis` | `LIQUIDITY_BUCKET` |
| `rst01_method` | `LARGEST_COMPLIANT_SALE` |
| `working_day_calendar` | `WEEKENDS_ONLY` |
| `art17_3_basis` | `STS_FLAG` |
| `public_debt_diversification` | `ART_17_7_CONDITIONS` |
| `nav_basis` | `SUM_OF_MARKET_VALUE` |

## How it is checked

The funds are synthetic, so there is no external figure to reconcile to. In its place, a
suite of 129 checks:

- **Position-level known answers.** Golden numbers pin each formula independently of the
  sample data: market value 54,104,799 at spread duration 0.293150685 and yield 0.037321488,
  under a 54bp shock, gives a credit loss of -82,567.
- **A synthetic breach of every Article 16 and 17 limit**, so each limit is known to fire.
- **The para 61 working-day windows and the FX pair maths**, including a Friday valuation
  reaching a Monday maturity in one working day, and sterling reached through the euro cross.
- **RST-01 is proved, not asserted.** The solver's answer is re-tested against the same
  compliance function at 401 points along the sale, and cut back if any point fails.
- **Every workbook the run writes is read back as raw XML**, because a file can be written
  that reads back happily in Python and still be refused by Excel.

## Sources

- Regulation (EU) 2017/1131 of the European Parliament and of the Council on money market
  funds, Article 28 in particular, and the portfolio rules in Articles 16, 17, 18, 24 and 25.
- ESMA Guidelines on stress test scenarios under the MMF Regulation, ESMA50-481369926-30848,
  paragraphs 50-65 and Tables 1-14.
