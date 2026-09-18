"""Query layer: filters run as SQL star joins against the DuckDB warehouse.

Every dashboard read goes through ``DashboardQuery``; results are cached per
(method, arguments, data version) and the cache is dropped whenever the live
status fact is replaced (upload, threshold change, reset).
"""
from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import Any, Callable

import duckdb
import pandas as pd

from common import STATUS_ORDER, VED_ORDER
from warehouse import RISK_STATUSES

SKU_ATTRS = "s.SKU_Name, s.Category, s.ROP_Confidence, s.ROP_Source, s.LeadTime_Months, s.AvgMonthlyDemand"
BRANCH_ATTRS = "b.BranchName, b.BranchGroup"


@dataclass(frozen=True)
class Filters:
    statuses: tuple[str, ...] = ()
    veds: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    confidences: tuple[str, ...] = ()
    branch: str | None = None  # BranchName; None = all branches
    critical_only: bool = False
    search: str = ""
    min_gap: float = 0.0

    def where(self) -> tuple[str, list[Any]]:
        """Parameterized WHERE clause over the columns of v_status / v_history."""
        clauses: list[str] = []
        params: list[Any] = []

        def isin(column: str, values: tuple[str, ...]) -> None:
            if values:
                clauses.append(f"{column} IN ({', '.join('?' for _ in values)})")
                params.extend(values)

        isin("Status", self.statuses)
        isin("VED", self.veds)
        isin("Category", self.categories)
        isin("ROP_Confidence", self.confidences)
        if self.branch:
            clauses.append("BranchName = ?")
            params.append(self.branch)
        if self.critical_only:
            isin("Status", RISK_STATUSES)
        term = self.search.strip().lower()
        if term:
            cols = ["SKU_Name", "CAST(SKU_ID AS VARCHAR)", "Supplier", "Manufacturer", "BranchName"]
            clauses.append("(" + " OR ".join(f"contains(lower(COALESCE({c}, '')), ?)" for c in cols) + ")")
            params.extend([term] * len(cols))
        if self.min_gap > 0:
            clauses.append("COALESCE(ROPGap, 0) >= ?")
            params.append(float(self.min_gap))
        return (" AND ".join(clauses) or "TRUE"), params


def cached(method: Callable) -> Callable:
    @functools.wraps(method)
    def wrapper(self: "DashboardQuery", *args: Any) -> Any:
        key = (method.__name__, args, self.version)
        if key not in self._cache:
            self._cache[key] = method(self, *args)
        return self._cache[key]
    return wrapper


class DashboardQuery:
    def __init__(self, con: duckdb.DuckDBPyConnection):
        self.con = con
        self.version = 0
        self._cache: dict[tuple, Any] = {}
        self._create_views()

    # ------------------------------------------------------------------ setup
    def _create_views(self) -> None:
        for view, fact in [("v_status", "fact_sku_status"), ("v_history", "fact_status_history")]:
            self.con.execute(f"""
                CREATE OR REPLACE VIEW {view} AS
                SELECT f.*, {SKU_ATTRS}, {BRANCH_ATTRS}
                FROM {fact} f
                JOIN dim_sku s USING (SKU_ID)
                LEFT JOIN dim_branch b USING (Branch_ID)
            """)

    def replace_table(self, name: str, frame: pd.DataFrame) -> None:
        """Swap a warehouse table in this in-memory connection and drop the cache."""
        self.con.register("_incoming", frame)
        self.con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM _incoming")
        self.con.unregister("_incoming")
        self._create_views()
        self.version += 1
        self._cache.clear()

    def df(self, sql: str, params: list[Any] | None = None) -> pd.DataFrame:
        return self.con.execute(sql, params or []).df()

    def _filtered(self, f: Filters, view: str = "v_status") -> tuple[str, list[Any]]:
        where, params = f.where()
        return f"FROM {view} WHERE {where}", params

    def table_frame(self, name: str) -> pd.DataFrame:
        return self.df(f"SELECT * FROM {name}")

    # ------------------------------------------------------------ dimensions
    @cached
    def filter_options(self) -> dict[str, list[str]]:
        categories = self.df("SELECT Category FROM dim_category ORDER BY CategoryOrder")["Category"].tolist()
        branches = self.df("SELECT DISTINCT BranchName FROM v_status WHERE BranchName IS NOT NULL ORDER BY 1")["BranchName"].tolist()
        confidences = self.df("SELECT DISTINCT ROP_Confidence FROM dim_sku WHERE ROP_Confidence IS NOT NULL ORDER BY 1")["ROP_Confidence"].tolist()
        return {"categories": categories, "branches": branches, "confidences": confidences}

    @cached
    def max_gap(self) -> float:
        return float(self.con.execute("SELECT COALESCE(max(ROPGap), 0) FROM fact_sku_status").fetchone()[0])

    @cached
    def scope(self) -> str:
        row = self.con.execute("SELECT any_value(Scope) FROM fact_sku_status").fetchone()
        return str(row[0]) if row and row[0] else "NETWORK"

    @cached
    def sku_detail(self, sku_id: int) -> dict[str, Any]:
        rows = self.df("SELECT LeadTime_Months, AvgMonthlyDemand, ROP_Source FROM dim_sku WHERE SKU_ID = ?", [int(sku_id)])
        return rows.iloc[0].to_dict() if len(rows) else {}

    # ------------------------------------------------------------- snapshot
    @cached
    def kpis(self, f: Filters) -> dict[str, float]:
        frm, params = self._filtered(f)
        row = self.df(f"""
            SELECT count(DISTINCT SKU_ID) AS sku_count,
                   COALESCE(sum(InventoryPosition), 0) AS inventory_position,
                   COALESCE(sum(ROP), 0) AS total_rop,
                   COALESCE(sum(ROPGap), 0) AS rop_gap,
                   COALESCE(sum(CAST(IsCritical AS INTEGER)), 0) AS critical
            {frm}
        """, params).iloc[0]
        return {k: float(v) for k, v in row.items()}

    @cached
    def status_counts(self, f: Filters) -> pd.Series:
        frm, params = self._filtered(f)
        counts = self.df(f"SELECT Status, count(*) AS n {frm} GROUP BY Status", params)
        return counts.set_index("Status")["n"].reindex(STATUS_ORDER, fill_value=0).astype(int)

    @cached
    def ved_summary(self, f: Filters) -> pd.DataFrame:
        frm, params = self._filtered(f)
        out = self.df(f"""
            SELECT VED,
                   sum(CASE WHEN Status IN ('{RISK_STATUSES[0]}', '{RISK_STATUSES[1]}') THEN 1 ELSE 0 END) AS Critical,
                   COALESCE(sum(ROPGap), 0) AS Gap
            {frm} GROUP BY VED
        """, params)
        return out.set_index("VED").reindex(VED_ORDER, fill_value=0).reset_index()

    @cached
    def top_shortages(self, f: Filters, n: int = 20) -> pd.DataFrame:
        frm, params = self._filtered(f)
        return self.df(f"""
            SELECT SKU_Name, BranchName, ROPGap, WeightedGap, VED, Status, InventoryPosition, ROP
            {frm} AND Status IN (?, ?)
            ORDER BY WeightedGap DESC NULLS LAST, PriorityScore DESC NULLS LAST
            LIMIT {int(n)}
        """, params + list(RISK_STATUSES))

    @cached
    def density_points(self, f: Filters) -> pd.DataFrame:
        frm, params = self._filtered(f)
        return self.df(f"SELECT ROP, InventoryPosition {frm} AND COALESCE(ROP, 0) > 0", params)

    @cached
    def risk_points(self, f: Filters, limit: int = 3000) -> pd.DataFrame:
        frm, params = self._filtered(f)
        return self.df(f"""
            SELECT SKU_Name, ROP, InventoryPosition, ROPGap, VED, Status
            {frm} AND COALESCE(ROP, 0) > 0 AND Status IN (?, ?)
            ORDER BY PriorityScore DESC NULLS LAST
            LIMIT {int(limit)}
        """, params + list(RISK_STATUSES))

    @cached
    def table(self, f: Filters, limit: int = 1000) -> pd.DataFrame:
        frm, params = self._filtered(f)
        return self.df(f"""
            SELECT SKU_ID, SKU_Name, BranchName, VED, Category, Status, InventoryPosition, ROP, ROPGap,
                   CoverageRatio, ROP_Confidence, Supplier
            {frm}
            ORDER BY PriorityScore DESC NULLS LAST, WeightedGap DESC NULLS LAST
            LIMIT {int(limit)}
        """, params)

    @cached
    def coverage(self, f: Filters) -> pd.DataFrame:
        """Branch x category sums; MetricsCalculator.finalize_coverage turns them into ratios."""
        frm, params = self._filtered(f)
        return self.df(f"""
            WITH w AS (
                SELECT COALESCE(BranchName, 'Тодорхойгүй') AS BranchName, Category, SKU_ID,
                       COALESCE(ROP, 0) AS ROP,
                       COALESCE(InventoryPosition, 0) AS Inv,
                       COALESCE(ROP, 0) > 0 AS Eligible,
                       CASE WHEN COALESCE(ROP, 0) > 0 AND HasInventory
                            THEN least(greatest(COALESCE(InventoryPosition, 0) / ROP, 0), 1) ELSE 0 END AS Ratio
                {frm}
            )
            SELECT BranchName, Category,
                   count(DISTINCT SKU_ID) AS SKUCount,
                   sum(CASE WHEN Eligible AND Ratio >= 1 THEN 1 ELSE 0 END) AS CoveredSKU,
                   sum(CAST(Eligible AS INTEGER)) AS EligibleSKU,
                   sum(Ratio) AS CoverageRatio,
                   sum(ROP) AS ROP,
                   sum(Inv) AS Inventory
            FROM w GROUP BY BranchName, Category
        """, params)

    @cached
    def branch_exposure(self, n: int = 30) -> pd.DataFrame:
        return self.df(f"""
            SELECT b.BranchName AS Branch,
                   sum(r.ROP) AS ROP,
                   COALESCE(sum(r.ROP) FILTER (WHERE r.VED = 'V'), 0) AS VROP,
                   COALESCE(sum(r.ROP) FILTER (WHERE r.VED = 'E'), 0) AS EROP,
                   COALESCE(sum(r.ROP) FILTER (WHERE r.VED = 'D'), 0) AS DROP
            FROM fact_branch_rop r JOIN dim_branch b USING (Branch_ID)
            GROUP BY b.BranchName ORDER BY ROP DESC LIMIT {int(n)}
        """)

    @cached
    def quality(self) -> dict[str, int]:
        q = lambda sql: int(self.con.execute(sql).fetchone()[0] or 0)  # noqa: E731
        return {
            "missing_inventory": q("SELECT count(*) FROM fact_sku_status WHERE NOT HasInventory"),
            "missing_rop_sku": q("SELECT count(*) FROM dim_sku WHERE NOT HasROP"),
            "low_confidence_sku": q("SELECT count(*) FROM dim_sku WHERE ROP_Confidence = 'Бага'"),
        }

    # -------------------------------------------------------------- history
    @cached
    def snapshot_dates(self) -> pd.DataFrame:
        return self.df("""
            SELECT h.DateKey, d.Date, any_value(h.Scope) AS Scope, count(*) AS Rows
            FROM fact_status_history h JOIN dim_date d USING (DateKey)
            GROUP BY h.DateKey, d.Date ORDER BY h.DateKey
        """)

    @cached
    def trend(self, f: Filters, include_network: bool = False) -> pd.DataFrame:
        """One row per snapshot date x status with count, gap and critical V/E."""
        frm, params = self._filtered(f, "v_history")
        scope = "" if include_network else " AND Scope = 'BRANCH'"
        return self.df(f"""
            SELECT DateKey, Status, count(*) AS Count,
                   COALESCE(sum(ROPGap), 0) AS Gap,
                   sum(CAST(IsCritical AS INTEGER)) AS Critical,
                   any_value(Scope) AS Scope
            {frm}{scope}
            GROUP BY DateKey, Status ORDER BY DateKey
        """, params)

    @cached
    def movement(self, f: Filters, date_a: int, date_b: int, limit: int = 2000) -> pd.DataFrame:
        """SKU x branch rows whose status changed between two snapshots."""
        where, params = f.where()
        return self.df(f"""
            WITH b AS (SELECT * FROM v_history WHERE DateKey = ? AND {where}),
                 a AS (SELECT SKU_ID, Branch_ID, Status, InventoryPosition FROM fact_status_history WHERE DateKey = ?)
            SELECT b.SKU_ID, b.SKU_Name, b.BranchName, b.VED, b.Category,
                   a.Status AS StatusFrom, b.Status AS StatusTo,
                   a.InventoryPosition AS InventoryFrom, b.InventoryPosition AS InventoryTo,
                   b.ROP, b.ROPGap,
                   sb.Severity - sa.Severity AS SeverityChange,
                   CASE WHEN sb.Severity > sa.Severity THEN 'Муудсан'
                        WHEN sb.Severity < sa.Severity THEN 'Сайжирсан'
                        ELSE 'Өөрчлөгдсөн' END AS Direction
            FROM b JOIN a USING (SKU_ID, Branch_ID)
            JOIN dim_status sa ON sa.Status = a.Status
            JOIN dim_status sb ON sb.Status = b.Status
            WHERE a.Status <> b.Status
            ORDER BY SeverityChange DESC, b.WeightedGap DESC NULLS LAST
            LIMIT {int(limit)}
        """, [int(date_b)] + params + [int(date_a)])

    @cached
    def movement_summary(self, f: Filters, date_a: int, date_b: int) -> dict[str, int]:
        where, params = f.where()
        row = self.df(f"""
            WITH b AS (SELECT SKU_ID, Branch_ID, Status FROM v_history WHERE DateKey = ? AND {where}),
                 a AS (SELECT SKU_ID, Branch_ID, Status FROM fact_status_history WHERE DateKey = ?)
            SELECT count(*) FILTER (WHERE sb.Severity > sa.Severity) AS worse,
                   count(*) FILTER (WHERE sb.Severity < sa.Severity) AS better,
                   count(*) FILTER (WHERE a.Status <> b.Status) AS changed,
                   count(*) AS compared
            FROM b JOIN a USING (SKU_ID, Branch_ID)
            JOIN dim_status sa ON sa.Status = a.Status
            JOIN dim_status sb ON sb.Status = b.Status
        """, [int(date_b)] + params + [int(date_a)]).iloc[0]
        return {k: int(v or 0) for k, v in row.items()}
