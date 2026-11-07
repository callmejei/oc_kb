"""Synthetic DuckDB data marts with obviously fake values.

Identifiers and PII-shaped columns follow patterns no real record can match:
  names "Test Customer R-00042", emails "@example.invalid", phones "+00 ...", ids "FAKE-...".
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

import duckdb

BU_PREFIX = {"retail": "R", "corporate": "C", "wealth": "W"}
N_CUSTOMERS = {"retail": 400, "corporate": 120, "wealth": 150}
LOANS_PER_CUSTOMER = {"retail": 3, "corporate": 6}

DDL = """
CREATE SCHEMA mart_cust;
CREATE SCHEMA mart_credit;
CREATE SCHEMA mart_wealth;
CREATE TABLE mart_cust.customer (
  customer_id VARCHAR PRIMARY KEY, bu VARCHAR NOT NULL, full_name VARCHAR, email VARCHAR, phone VARCHAR,
  national_id VARCHAR, date_of_birth DATE, segment VARCHAR, country VARCHAR, onboarded_date DATE);
CREATE TABLE mart_credit.loan (
  loan_id VARCHAR PRIMARY KEY, customer_id VARCHAR NOT NULL, bu VARCHAR NOT NULL, product VARCHAR,
  principal DECIMAL(18,2), outstanding DECIMAL(18,2), interest_rate DECIMAL(6,3), origination_date DATE,
  maturity_date DATE, status VARCHAR);
CREATE TABLE mart_credit.collateral (
  collateral_id VARCHAR PRIMARY KEY, loan_id VARCHAR NOT NULL, bu VARCHAR NOT NULL, collateral_type VARCHAR,
  valuation DECIMAL(18,2), ltv_pct DECIMAL(6,2), valuation_date DATE);
CREATE TABLE mart_wealth.portfolio (
  portfolio_id VARCHAR PRIMARY KEY, client_ref VARCHAR, customer_id VARCHAR NOT NULL, bu VARCHAR NOT NULL,
  mandate VARCHAR, aum DECIMAL(18,2), equity_pct DECIMAL(5,2), bond_pct DECIMAL(5,2), cash_pct DECIMAL(5,2),
  risk_profile VARCHAR, as_of_date DATE);
"""

PRODUCTS = {
    "retail": ["Home Loan", "Car Loan", "Personal Loan", "Credit Card"],
    "corporate": ["Term Loan", "Trade Finance", "Working Capital", "Commercial Property Loan"],
}
COLLATERAL = {
    "Home Loan": "Residential property",
    "Car Loan": "Vehicle",
    "Commercial Property Loan": "Commercial property",
    "Term Loan": "Fixed charge",
    "Trade Finance": "Receivables",
    "Working Capital": "Inventory",
}


def build_marts(path: Path, seed: int = 20261107) -> dict[str, int]:
    rnd = random.Random(seed)  # noqa: S311
    path.parent.mkdir(parents=True, exist_ok=True)
    for p in (path, path.with_suffix(path.suffix + ".wal")):
        p.unlink(missing_ok=True)
    con = duckdb.connect(str(path))
    con.execute(DDL)
    customers, loans, collaterals, portfolios = [], [], [], []
    base = date(2026, 9, 30)
    for bu, n in N_CUSTOMERS.items():
        pfx = BU_PREFIX[bu]
        for i in range(1, n + 1):
            cid = f"CUST-{pfx}-{i:05d}"
            customers.append(
                (
                    cid,
                    bu,
                    f"Test Customer {pfx}-{i:05d}",
                    f"cust.{pfx.lower()}{i:05d}@example.invalid",
                    f"+00 0000 {i:04d}",
                    f"FAKE-ID-{pfx}{i:05d}",
                    date(1950, 1, 1) + timedelta(days=rnd.randrange(0, 365 * 50)),
                    rnd.choice(
                        {
                            "retail": ["Mass", "Affluent"],
                            "corporate": ["SME", "Mid-market", "Large corporate"],
                            "wealth": ["Private", "Premier"],
                        }[bu]
                    ),
                    "Exampleland",
                    base - timedelta(days=rnd.randrange(30, 3650)),
                )
            )
            for j in range(rnd.randint(0, LOANS_PER_CUSTOMER.get(bu, 0))):
                product = rnd.choice(PRODUCTS[bu])
                lid = f"LN-{pfx}-{i:05d}-{j}"
                principal = round(rnd.uniform(20_000, 900_000 if bu == "retail" else 8_000_000), -2)
                orig = base - timedelta(days=rnd.randrange(30, 3650))
                loans.append(
                    (
                        lid,
                        cid,
                        bu,
                        product,
                        principal,
                        round(principal * rnd.uniform(0.1, 1.0), 2),
                        round(rnd.uniform(1.8, 9.5), 3),
                        orig,
                        orig + timedelta(days=365 * rnd.choice([3, 5, 10, 25])),
                        rnd.choices(["CURRENT", "ARREARS_30", "ARREARS_90", "CLOSED"], [80, 8, 4, 8])[0],
                    )
                )
                if product in COLLATERAL:
                    valuation = round(principal / rnd.uniform(0.35, 0.85), -2)
                    collaterals.append(
                        (
                            f"COL-{lid}",
                            lid,
                            bu,
                            COLLATERAL[product],
                            valuation,
                            round(100 * principal / valuation, 2),
                            base - timedelta(days=rnd.randrange(0, 720)),
                        )
                    )
            if bu == "wealth":
                eq = rnd.choice([20, 45, 65, 80])
                bd = {20: 60, 45: 45, 65: 30, 80: 15}[eq]
                portfolios.append(
                    (
                        f"PF-W-{i:05d}",
                        f"CLIENT-REF-{i:05d}",
                        cid,
                        bu,
                        rnd.choice(["Discretionary", "Advisory", "Execution only"]),
                        round(rnd.uniform(250_000, 25_000_000), 2),
                        eq,
                        bd,
                        100 - eq - bd,
                        {20: "Conservative", 45: "Balanced", 65: "Growth", 80: "Aggressive"}[eq],
                        base,
                    )
                )
    con.executemany("INSERT INTO mart_cust.customer VALUES (?,?,?,?,?,?,?,?,?,?)", customers)
    con.executemany("INSERT INTO mart_credit.loan VALUES (?,?,?,?,?,?,?,?,?,?)", loans)
    con.executemany("INSERT INTO mart_credit.collateral VALUES (?,?,?,?,?,?,?)", collaterals)
    con.executemany("INSERT INTO mart_wealth.portfolio VALUES (?,?,?,?,?,?,?,?,?,?,?)", portfolios)
    con.execute("CHECKPOINT")
    con.close()
    return {
        "mart_cust.customer": len(customers),
        "mart_credit.loan": len(loans),
        "mart_credit.collateral": len(collaterals),
        "mart_wealth.portfolio": len(portfolios),
    }
