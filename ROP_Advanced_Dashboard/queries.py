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

from common import STATUS_ORDER
from warehouse import RISK_STATUSES

TOTAL = "НИЙТ"
UNKNOWN_CLASS = "Тодорхойгүй"
CLASS_ORDER = {"ABC": ["A", "B", "C", UNKNOWN_CLASS], "XYZ": ["X", "Y", "Z", UNKNOWN_CLASS]}
CLASS_SOURCE_ROP = "ROP файл"
CLASS_SOURCE_FILE = "ABC XYZ файл"
# Batch expiry statuses of a valid future date, most urgent first (expiry.py).
FUTURE_EXPIRY_STATUSES = ["CRITICAL", "WARNING", "WATCH", "OK"]
COVERAGE_LEVELS = {
    "branch_category": ["BranchName", "Category"],
    "branch": ["BranchName"],
    "category": ["Category"],
    "sku": ["SKU_ID", "SKU_Name", "Category"],
}
TOP_MEASURES = {
    "gap": "COALESCE(WeightedGap, 0)",
    "excess": "CASE WHEN ROP > 0 AND InventoryPosition > ROP THEN InventoryPosition - ROP ELSE 0 END",
    "stock": "greatest(COALESCE(InventoryPosition, 0), 0)",
    "missing": "CASE WHEN NOT HasInventory THEN COALESCE(ROP, 0) ELSE 0 END",
}
# Stock / ROP bands of the SKU анализ tab, in display order. «1-2×» is the
# «Хэвийн» band at the default excess threshold of 2.
COVERAGE_BANDS = ["OUT", "LOW", "BELOW", "OK", "HIGH", "OVER", "NOROP", "NODATA"]
BAND_SQL = """CASE WHEN NOT HasInventory THEN 'NODATA'
     WHEN COALESCE(ROP, 0) <= 0 THEN 'NOROP'
     WHEN COALESCE(InventoryPosition, 0) <= 0 THEN 'OUT'
     WHEN InventoryPosition < 0.5 * ROP THEN 'LOW'
     WHEN InventoryPosition < ROP THEN 'BELOW'
     WHEN InventoryPosition < 2 * ROP THEN 'OK'
     WHEN InventoryPosition < 3 * ROP THEN 'HIGH'
     ELSE 'OVER' END"""
SKU_ATTRS = "s.SKU_Name, s.Category, s.ROP_Confidence, s.ROP_Source, s.LeadTime_Months, s.AvgMonthlyDemand"
BRANCH_ATTRS = "b.BranchName, b.BranchGroup"


@dataclass(frozen=True)
class Filters:
    statuses: tuple[str, ...] = ()
    veds: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    branch: str | None = None  # BranchName; None = all branches
    critical_only: bool = False
    search: str = ""
    min_gap: float = 0.0
    # ABC / XYZ tab only: these columns exist in v_abc_xyz, not in v_status / v_history.
    abc: tuple[str, ...] = ()
    xyz: tuple[str, ...] = ()
    # SKU анализ tab only: one of COVERAGE_BANDS.
    band: str = ""

    def where(self) -> tuple[str, list[Any]]:
        """Parameterized WHERE clause over the columns of v_status / v_history
        (and v_abc_xyz for the ABC / XYZ classes)."""
        clauses: list[str] = []
        params: list[Any] = []

        def isin(column: str, values: tuple[str, ...]) -> None:
            if values:
                clauses.append(f"{column} IN ({', '.join('?' for _ in values)})")
                params.extend(values)

        isin("Status", self.statuses)
        isin("VED", self.veds)
        isin("Category", self.categories)
        isin("ABC", self.abc)
        isin("XYZ", self.xyz)
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
        if self.band:
            clauses.append(f"({BAND_SQL}) = ?")
            params.append(self.band)
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
        self.active_branches: tuple[str, ...] = ()
        self._create_views()

    def replace_connection(self, con: duckdb.DuckDBPyConnection) -> None:
        """Switch to a freshly loaded warehouse (the dashboard's live refresh);
        the active branch list carries over."""
        old, self.con = self.con, con
        self.set_active_branches(self.active_branches)
        old.close()

    def set_active_branches(self, names: tuple[str, ...]) -> None:
        """Limit every status view to these BranchNames («Салбарын жагсаалт»);
        () = every branch. NETWORK rows are not branches and always stay."""
        self.active_branches = tuple(names)
        self.con.register("_active", pd.DataFrame({"BranchName": pd.Series(self.active_branches, dtype="string")}))
        self.con.execute("CREATE OR REPLACE TABLE active_branch AS SELECT * FROM _active")
        self.con.unregister("_active")
        self._create_views()
        self.version += 1
        self._cache.clear()

    # ------------------------------------------------------------------ setup
    def tables(self) -> set[str]:
        return {r[0] for r in self.con.execute("SELECT table_name FROM information_schema.tables").fetchall()}

    def _create_views(self) -> None:
        tables = self.tables()
        # ROP reporting covers only SKU x branch rows with an average monthly
        # sales figure above 0 (the branch's in fact_branch_rop, the SKU's in
        # dim_sku for NETWORK rows). The rest - stock the ROP workbook does not
        # list, and rows it lists with 0 sales - get no ROP and are left out of
        # every status count, «ROP байхгүй» and «Өгөгдөл алга» included.
        branch_sales = ("SELECT SKU_ID, Branch_ID FROM fact_branch_rop WHERE AvgMonthlyDemand_Branch > 0 UNION ALL "
                        if "fact_branch_rop" in tables else "")
        self.con.execute(f"""
            CREATE OR REPLACE VIEW v_has_sales AS
            {branch_sales}SELECT SKU_ID, 'NETWORK' AS Branch_ID FROM dim_sku WHERE AvgMonthlyDemand > 0
        """)
        # Active branches only, when set_active_branches gave a list.
        active = (" AND (f.Branch_ID = 'NETWORK' OR b.BranchName IN (SELECT BranchName FROM active_branch))"
                  if self.active_branches else "")
        for view, fact in [("v_status", "fact_sku_status"), ("v_history", "fact_status_history")]:
            self.con.execute(f"""
                CREATE OR REPLACE VIEW {view} AS
                SELECT f.*, {SKU_ATTRS}, {BRANCH_ATTRS}
                FROM {fact} f
                JOIN dim_sku s USING (SKU_ID)
                LEFT JOIN dim_branch b USING (Branch_ID)
                WHERE EXISTS (SELECT 1 FROM v_has_sales h WHERE h.SKU_ID = f.SKU_ID AND h.Branch_ID = f.Branch_ID){active}
            """)
        # The branch ABC / XYZ class of each status row. A separate view because
        # DuckDB keeps an unused LEFT JOIN, which would double the cost of every
        # v_status query. The ROP workbook's class first, then «ABC XYZ.xlsx»
        # (fact_abc_xyz_file); rows neither classifies are UNKNOWN_CLASS.
        known = {dim: ", ".join(f"'{c}'" for c in order if c != UNKNOWN_CLASS) for dim, order in CLASS_ORDER.items()}
        file_classes = ("fact_abc_xyz_file" if "fact_abc_xyz_file" in tables else
                        "(SELECT NULL::BIGINT AS SKU_ID, NULL::VARCHAR AS Branch_ID, NULL::VARCHAR AS ABC, NULL::VARCHAR AS XYZ WHERE FALSE)")
        self.con.execute(f"""
            CREATE OR REPLACE VIEW v_abc_xyz AS
            SELECT v.*,
                   CASE WHEN r.ABC IN ({known['ABC']}) THEN r.ABC
                        WHEN a.ABC IN ({known['ABC']}) THEN a.ABC ELSE '{UNKNOWN_CLASS}' END AS ABC,
                   CASE WHEN r.XYZ IN ({known['XYZ']}) THEN r.XYZ
                        WHEN a.XYZ IN ({known['XYZ']}) THEN a.XYZ ELSE '{UNKNOWN_CLASS}' END AS XYZ,
                   CASE WHEN r.ABC IN ({known['ABC']}) AND r.XYZ IN ({known['XYZ']}) THEN '{CLASS_SOURCE_ROP}'
                        WHEN a.ABC IS NOT NULL THEN '{CLASS_SOURCE_FILE}' END AS ClassSource,
                   r.Sales7M
            FROM v_status v
            -- class columns as text: an all-empty column can arrive typed as a number
            LEFT JOIN (SELECT SKU_ID, Branch_ID, CAST(ABC AS VARCHAR) AS ABC, CAST(XYZ AS VARCHAR) AS XYZ, Sales7M
                       FROM fact_branch_rop) r USING (SKU_ID, Branch_ID)
            LEFT JOIN (SELECT SKU_ID, Branch_ID, CAST(ABC AS VARCHAR) AS ABC, CAST(XYZ AS VARCHAR) AS XYZ
                       FROM {file_classes}) a USING (SKU_ID, Branch_ID)
        """)
        # Batches with a valid expiry date still ahead (DTE >= 0) and stock on
        # hand. Every SKU, with or without sales: stock expires either way.
        batches = "fact_batch_expiry" if "fact_batch_expiry" in tables else (
            "(SELECT NULL::BIGINT AS SKU_ID, NULL::VARCHAR AS Branch_ID, NULL::DATE AS ExpiryDate, NULL::INTEGER AS DTE, "
            "NULL::VARCHAR AS ExpiryStatus, NULL::DOUBLE AS Qty, NULL::INTEGER AS FEFORank, NULL::DOUBLE AS ProjectedWaste, "
            "NULL::BOOLEAN AS AtRisk, NULL::BOOLEAN AS PullFlag, NULL::DOUBLE AS StockValue, NULL::INTEGER AS DateKey WHERE FALSE)")
        self.con.execute(f"""
            CREATE OR REPLACE VIEW v_expiry AS
            SELECT e.SKU_ID, e.Branch_ID, CAST(e.ExpiryDate AS DATE) AS ExpiryDate, e.DTE, e.ExpiryStatus, e.Qty,
                   e.FEFORank, e.ProjectedWaste, e.AtRisk, e.PullFlag, e.StockValue, e.DateKey,
                   s.SKU_Name, s.Category, s.VED, b.BranchName, b.BranchGroup,
                   CAST(NULL AS VARCHAR) AS Supplier, CAST(NULL AS VARCHAR) AS Manufacturer
            FROM {batches} e
            JOIN dim_sku s USING (SKU_ID)
            LEFT JOIN dim_branch b USING (Branch_ID)
            WHERE e.ExpiryDate IS NOT NULL AND e.DTE >= 0 AND e.Qty > 0
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
        return {"categories": categories, "branches": branches}

    @cached
    def max_gap(self) -> float:
        return float(self.con.execute("SELECT COALESCE(max(ROPGap), 0) FROM v_status").fetchone()[0])

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
                   COALESCE(sum(CAST(IsCritical AS INTEGER)), 0) AS critical,
                   COALESCE(sum(CASE WHEN ROP > 0 AND InventoryPosition > ROP THEN InventoryPosition - ROP END), 0) AS excess
            {frm}
        """, params).iloc[0]
        return {k: float(v) for k, v in row.items()}

    @cached
    def status_counts(self, f: Filters) -> pd.Series:
        frm, params = self._filtered(f)
        counts = self.df(f"SELECT Status, count(*) AS n {frm} GROUP BY Status", params)
        return counts.set_index("Status")["n"].reindex(STATUS_ORDER, fill_value=0).astype(int)

    @cached
    def ved_status(self, f: Filters) -> pd.DataFrame:
        """SKU x branch rows per VED x Status, with ROP gap and excess above ROP."""
        frm, params = self._filtered(f)
        return self.df(f"""
            SELECT COALESCE(VED, 'Тодорхойгүй') AS VED, Status, count(*) AS Count,
                   COALESCE(sum(ROPGap), 0) AS Gap,
                   COALESCE(sum(CASE WHEN ROP > 0 AND InventoryPosition > ROP THEN InventoryPosition - ROP END), 0) AS Excess
            {frm} GROUP BY 1, 2
        """, params)

    @cached
    def top_items(self, f: Filters, measure: str = "gap", level: str = "row", n: int = 20) -> pd.DataFrame:
        """Top SKU x branch rows (level='row') or SKUs summed over branches (level='sku').

        measure: 'gap'    VED-weighted ROP gap (shortage)
                 'excess' InventoryPosition - ROP where ROP > 0 (stock above ROP)
                 'stock'  InventoryPosition (e.g. stock held without a ROP)
                 'missing' ROP of rows with no inventory record
        """
        expr = TOP_MEASURES[measure]
        frm, params = self._filtered(f)
        if level == "sku":
            sql = f"""
                SELECT SKU_ID, any_value(SKU_Name) AS SKU_Name, mode(VED) AS VED,
                       count(DISTINCT BranchName) AS Branches, CAST(NULL AS VARCHAR) AS BranchName, CAST(NULL AS VARCHAR) AS Status,
                       sum(InventoryPosition) AS InventoryPosition, sum(ROP) AS ROP,
                       COALESCE(sum(ROPGap), 0) AS ROPGap, sum({expr}) AS Value
                {frm} AND {expr} > 0
                GROUP BY SKU_ID ORDER BY Value DESC LIMIT {int(n)}
            """
        else:
            sql = f"""
                SELECT SKU_ID, SKU_Name, VED, 1 AS Branches, BranchName, Status,
                       InventoryPosition, ROP, ROPGap, {expr} AS Value
                {frm} AND {expr} > 0
                ORDER BY Value DESC, PriorityScore DESC NULLS LAST LIMIT {int(n)}
            """
        return self.df(sql, params)

    @cached
    def coverage_bands(self, f: Filters) -> pd.DataFrame:
        """SKU x branch rows per stock / ROP band x VED, with the units short
        of ROP and the units above it."""
        frm, params = self._filtered(f)
        return self.df(f"""
            SELECT {BAND_SQL} AS Band, COALESCE(VED, 'Тодорхойгүй') AS VED, count(*) AS Rows,
                   count(DISTINCT SKU_ID) AS SKUs,
                   COALESCE(sum(ROPGap), 0) AS Gap,
                   COALESCE(sum(CASE WHEN ROP > 0 AND InventoryPosition > ROP THEN InventoryPosition - ROP END), 0) AS Excess
            {frm} GROUP BY 1, 2
        """, params)

    @cached
    def table(self, f: Filters, limit: int = 1000) -> pd.DataFrame:
        frm, params = self._filtered(f)
        return self.df(f"""
            SELECT SKU_ID, SKU_Name, BranchName, VED, Category, Status, InventoryPosition, ROP, ROPGap,
                   CoverageRatio, {BAND_SQL} AS Band, ROP_Confidence, Supplier
            {frm}
            ORDER BY PriorityScore DESC NULLS LAST, WeightedGap DESC NULLS LAST
            LIMIT {int(limit)}
        """, params)

    @cached
    def coverage_summary(self, f: Filters, level: str = "branch_category") -> pd.DataFrame:
        """Coverage sums per group plus totals (GROUPING SETS); rows whose
        group column is the TOTAL label are the margins / grand total.
        MetricsCalculator.finalize_coverage turns the sums into ratios."""
        dims = COVERAGE_LEVELS[level]
        labels = ", ".join(
            f"CASE WHEN GROUPING({d}) = 1 THEN '{TOTAL}' ELSE CAST({d} AS VARCHAR) END AS {d}" for d in dims
        )
        grouping = " + ".join(f"GROUPING({d})" for d in dims)
        sets = "(" + ", ".join(dims) + "), " + ("".join(f"({d}), " for d in dims) if level == "branch_category" else "") + "()"
        frm, params = self._filtered(f)
        return self.df(f"""
            WITH w AS (
                SELECT COALESCE(BranchName, 'Тодорхойгүй') AS BranchName, Category, SKU_ID, SKU_Name,
                       COALESCE(ROP, 0) > 0 AS Eligible,
                       CASE WHEN COALESCE(ROP, 0) > 0 THEN ROP ELSE 0 END AS ROPe,
                       CASE WHEN COALESCE(ROP, 0) > 0 AND HasInventory
                            THEN least(greatest(COALESCE(InventoryPosition, 0), 0), ROP) ELSE 0 END AS Filled,
                       COALESCE(InventoryPosition, 0) AS Inv
                {frm}
            )
            SELECT {labels},
                   ({grouping}) AS TotalLevel,
                   count(*) AS Rows,
                   count(DISTINCT SKU_ID) AS SKUCount,
                   count(DISTINCT BranchName) AS BranchCount,
                   sum(CAST(Eligible AS INTEGER)) AS EligibleRows,
                   sum(CASE WHEN Eligible AND Filled >= ROPe THEN 1 ELSE 0 END) AS CoveredRows,
                   sum(CASE WHEN Eligible THEN Filled / ROPe ELSE 0 END) AS RatioSum,
                   sum(ROPe) AS ROP,
                   sum(Filled) AS Filled,
                   sum(Inv) AS Inventory
            FROM w GROUP BY GROUPING SETS ({sets})
        """, params)

    @cached
    def class_breakdown(self, f: Filters) -> pd.DataFrame:
        """Additive sums per ABC x XYZ x Status (at most 4 x 4 x 6 rows);
        MetricsCalculator.class_rollup rolls them up to either dimension.
        One plain GROUP BY is ~3x faster than GROUPING SETS here, and every
        measure is additive - a distinct SKU count is not (a SKU can be A at
        one branch and C at another), so there is none."""
        risk = ", ".join(f"'{s}'" for s in RISK_STATUSES)
        frm, params = self._filtered(f, "v_abc_xyz")
        return self.df(f"""
            SELECT ABC, XYZ, Status,
                   count(*) AS Rows,
                   sum(CAST(COALESCE(ROP, 0) > 0 AS INTEGER)) AS EligibleRows,
                   sum(CAST(COALESCE(ROP, 0) > 0 AND HasInventory AND InventoryPosition >= ROP AS INTEGER)) AS CoveredRows,
                   sum(CAST(Status IN ({risk}) AS INTEGER)) AS RiskRows,
                   sum(COALESCE(ROP, 0)) AS ROP,
                   sum(COALESCE(InventoryPosition, 0)) AS Inventory,
                   sum(COALESCE(ROPGap, 0)) AS Gap,
                   sum(CASE WHEN ROP > 0 AND InventoryPosition > ROP THEN InventoryPosition - ROP ELSE 0 END) AS Excess,
                   sum(COALESCE(Sales7M, 0)) AS Sales
            {frm}
            GROUP BY ABC, XYZ, Status
        """, params)

    @cached
    def class_items(self, f: Filters, order: str = "ABC", limit: int = 2000) -> pd.DataFrame:
        """SKU x branch rows with their ABC / XYZ class, sorted by ``order``
        ('ABC': A-X first, 'XYZ': X-A first), the most urgent first within a class."""
        if order not in CLASS_ORDER:
            raise ValueError(f"unknown class column {order!r}")
        other = "XYZ" if order == "ABC" else "ABC"
        frm, params = self._filtered(f, "v_abc_xyz")
        return self.df(f"""
            SELECT SKU_ID, SKU_Name, BranchName, ABC, XYZ, ClassSource, VED, Category, Status,
                   InventoryPosition, ROP, ROPGap, CoverageRatio, Sales7M
            {frm}
            ORDER BY {order}, {other}, PriorityScore DESC NULLS LAST, WeightedGap DESC NULLS LAST
            LIMIT {int(limit)}
        """, params)

    @cached
    def branch_exposure(self, n: int = 30) -> pd.DataFrame:
        """Branch ROP split by VED: the active branches, or the ``n`` largest
        when no branch list is set."""
        active = "WHERE b.BranchName IN (SELECT BranchName FROM active_branch)" if self.active_branches else ""
        limit = "" if self.active_branches else f"LIMIT {int(n)}"
        return self.df(f"""
            SELECT b.BranchName AS Branch,
                   sum(r.ROP) AS ROP,
                   COALESCE(sum(r.ROP) FILTER (WHERE r.VED = 'V'), 0) AS VROP,
                   COALESCE(sum(r.ROP) FILTER (WHERE r.VED = 'E'), 0) AS EROP,
                   COALESCE(sum(r.ROP) FILTER (WHERE r.VED = 'D'), 0) AS DROP
            FROM fact_branch_rop r JOIN dim_branch b USING (Branch_ID)
            {active}
            GROUP BY b.BranchName ORDER BY ROP DESC {limit}
        """)

    @cached
    def quality(self) -> dict[str, int]:
        q = lambda sql: int(self.con.execute(sql).fetchone()[0] or 0)  # noqa: E731
        return {
            "missing_inventory": q("SELECT count(*) FROM v_status WHERE NOT HasInventory"),
            # SKUs that sell but have no ROP; SKUs without sales get no ROP by design.
            "missing_rop_sku": q("SELECT count(*) FROM dim_sku WHERE NOT HasROP AND AvgMonthlyDemand > 0"),
            "low_confidence_sku": q("SELECT count(*) FROM dim_sku WHERE ROP_Confidence = 'Бага'"),
        }

    # --------------------------------------------------------------- expiry
    @staticmethod
    def _expiry_where(f: Filters, branches: tuple[str, ...], statuses: tuple[str, ...]) -> tuple[str, list[Any]]:
        """The Хугацаат tab's filter: the global VED / category / search, the
        tab's own branch list and expiry statuses. The ROP filters (status,
        critical, gap, the global branch) do not apply to batches."""
        where, params = Filters(veds=f.veds, categories=f.categories, search=f.search).where()
        for column, values in [("BranchName", branches), ("ExpiryStatus", statuses)]:
            if values:
                where += f" AND {column} IN ({', '.join('?' for _ in values)})"
                params += list(values)
        return where, params

    @cached
    def expiry_kpis(self, f: Filters, branches: tuple[str, ...] = (), statuses: tuple[str, ...] = ()) -> dict[str, float]:
        where, params = self._expiry_where(f, branches, statuses)
        row = self.df(f"""
            SELECT count(*) AS batches, count(DISTINCT SKU_ID) AS skus, count(DISTINCT BranchName) AS branches,
                   COALESCE(sum(Qty), 0) AS qty,
                   COALESCE(sum(Qty) FILTER (WHERE ExpiryStatus = 'CRITICAL'), 0) AS critical_qty,
                   count(*) FILTER (WHERE ExpiryStatus = 'CRITICAL') AS critical_batches,
                   COALESCE(sum(Qty) FILTER (WHERE ExpiryStatus = 'WARNING'), 0) AS warning_qty,
                   count(*) FILTER (WHERE AtRisk) AS at_risk_batches,
                   COALESCE(sum(ProjectedWaste), 0) AS projected_waste,
                   COALESCE(sum(StockValue), 0) AS stock_value,
                   min(ExpiryDate) AS nearest
            FROM v_expiry WHERE {where}
        """, params).iloc[0]
        return {k: (v if k == "nearest" else float(v or 0)) for k, v in row.items()}

    @cached
    def expiry_by_branch(self, f: Filters, branches: tuple[str, ...] = (), statuses: tuple[str, ...] = ()) -> pd.DataFrame:
        """One row per branch: batches, SKUs, quantity per expiry status, the
        nearest expiry date and the at-risk batches."""
        where, params = self._expiry_where(f, branches, statuses)
        per_status = ", ".join(
            f"COALESCE(sum(Qty) FILTER (WHERE ExpiryStatus = '{s}'), 0) AS \"{s}\"" for s in FUTURE_EXPIRY_STATUSES)
        return self.df(f"""
            SELECT BranchName, any_value(BranchGroup) AS BranchGroup,
                   count(*) AS Batches, count(DISTINCT SKU_ID) AS SKUs, sum(Qty) AS Qty, {per_status},
                   min(ExpiryDate) AS Nearest, count(*) FILTER (WHERE AtRisk) AS AtRisk
            FROM v_expiry WHERE {where}
            GROUP BY BranchName
        """, params)

    @cached
    def expiry_months(self, f: Filters, branches: tuple[str, ...] = (), statuses: tuple[str, ...] = (), months: int = 24) -> pd.DataFrame:
        """Quantity per expiry month x status for the next ``months`` months;
        later dates are one bucket (Month = NULL)."""
        where, params = self._expiry_where(f, branches, statuses)
        return self.df(f"""
            WITH m AS (SELECT CASE WHEN date_diff('month', date_trunc('month', current_date), ExpiryDate) < {int(months)}
                                   THEN date_trunc('month', ExpiryDate) END AS Month, ExpiryStatus, Qty
                       FROM v_expiry WHERE {where})
            SELECT Month, ExpiryStatus, sum(Qty) AS Qty, count(*) AS Batches FROM m GROUP BY ALL ORDER BY Month NULLS LAST
        """, params)

    @cached
    def expiry_items(self, f: Filters, branches: tuple[str, ...] = (), statuses: tuple[str, ...] = (), limit: int = 3000) -> pd.DataFrame:
        """Batches, the nearest expiry first."""
        where, params = self._expiry_where(f, branches, statuses)
        return self.df(f"""
            SELECT SKU_ID, SKU_Name, BranchName, Category, VED, ExpiryDate, DTE, ExpiryStatus, Qty,
                   FEFORank, ProjectedWaste, AtRisk, StockValue
            FROM v_expiry WHERE {where}
            ORDER BY DTE, Qty DESC, SKU_ID, BranchName
            LIMIT {int(limit)}
        """, params)

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
