"""Star-schema warehouse: Parquet tables + DuckDB.

Facts hold keys and measures only; descriptive attributes live in the
dimensions and are joined back in by the query layer (queries.py).

    python warehouse.py build [--categories "SKU category.csv"] [--ved "VED ангилал.csv"] [--powerbi]
    python warehouse.py history
    python warehouse.py check
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from common import (
    RISK_STATUSES,
    STATUS_ORDER,
    VED_FILE_SOURCE,
    VED_ORDER,
    excluded_mask,
    apply_category_map,
    apply_ved_map,
    branch_group,
    clean_category,
    is_excluded_branch,
    load_category_map,
    load_project_data,
    load_ved_map,
    save_json,
    ved_measures,
)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SNAPSHOT_DIR = BASE_DIR / "snapshots"
POWERBI_DIR = BASE_DIR / "powerbi_data"
DB_PATH = DATA_DIR / "warehouse.duckdb"

STATUS_FACT_COLUMNS = [
    # keys
    "SKU_ID", "Branch_ID", "DateKey",
    # measures
    "OnHand", "OnOrder", "Backorder", "InventoryPosition", "ROP", "ROPGap",
    "CoverageRatio", "WeightedGap", "PriorityScore", "InventoryLines",
    # degenerate attributes of the SKU x branch x date grain
    "Status", "VED", "DataMatch", "IsCritical", "HasROP", "HasInventory",
    "Supplier", "Manufacturer", "Scope",
]
BRANCH_ROP_COLUMNS = [
    "SKU_ID", "Branch_ID", "ROP", "AvgMonthlyDemand_Branch", "DemandSD", "LeadTimeMean",
    "LeadTimeSD", "Sales7M", "ABC", "XYZ", "ABC_XYZ", "Z_VED", "Z_ABCXYZ", "Z_Final", "VED", "VED_ROP", "HasROP",
]
FLOAT_COLUMNS = [
    "OnHand", "OnOrder", "Backorder", "InventoryPosition", "ROP", "ROPGap",
    "CoverageRatio", "WeightedGap", "PriorityScore", "InventoryLines",
]
BOOL_COLUMNS = ["IsCritical", "HasROP", "HasInventory"]
TEXT_COLUMNS = ["Status", "VED", "DataMatch", "Supplier", "Manufacturer", "Scope"]

# Tables that make up the warehouse, in load order.
TABLES = [
    "dim_sku", "dim_branch", "dim_date", "dim_status", "dim_ved", "dim_category",
    "fact_branch_rop", "fact_abc_xyz_file", "fact_sku_status", "fact_status_history", "fact_inventory_snapshot",
    "dim_expiry_status", "fact_batch_expiry", "fact_batch_expiry_history",
    "alias_mapping", "mapping_review", "data_dictionary",
]


# --------------------------------------------------------------------------
# Shaping
# --------------------------------------------------------------------------
def date_key(value: Any) -> int:
    return int(pd.Timestamp(value).strftime("%Y%m%d"))


def date_keys(dates: pd.Series) -> pd.Series:
    """yyyymmdd integer keys, parsing each distinct date string only once."""
    codes, uniques = pd.factorize(dates)
    parsed = pd.to_datetime(pd.Series(uniques))
    keys = (parsed.dt.year * 10000 + parsed.dt.month * 100 + parsed.dt.day).to_numpy()
    return pd.Series(keys[codes], index=dates.index)


def _as_bool(series: pd.Series) -> pd.Series:
    if series.dtype == bool:
        return series
    lookup = {True: True, 1: True, 1.0: True, "True": True, "true": True, "TRUE": True, "1": True, "1.0": True}
    return series.map(lambda v: lookup.get(v, False) if isinstance(v, (bool, int, float, str)) else False).astype(bool)


def _dedupe_keys(frame: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """The ROP source occasionally repeats a SKU x branch row with ROP 0.
    Keep the row with the highest ROP so the fact grain stays unique."""
    frame = frame.sort_values("ROP", ascending=False, na_position="last", kind="stable")
    return frame.drop_duplicates(keys, keep="first").sort_index()


def to_fact_status(wide: pd.DataFrame, snapshot_date: Any | None = None) -> pd.DataFrame:
    """Slim the wide output of ``common.build_status_snapshot`` into the fact grain."""
    df = wide
    if "BranchName" in df:
        df = df.loc[~excluded_mask(df["BranchName"])]
    df = df.copy()
    if "DateKey" not in df:
        dates = df["SnapshotDate"] if "SnapshotDate" in df else pd.Series(snapshot_date, index=df.index)
        if snapshot_date is not None:
            dates = dates.fillna(snapshot_date)
        df["DateKey"] = date_keys(dates)
    for col in STATUS_FACT_COLUMNS:
        if col not in df:
            df[col] = np.nan
    df = df[STATUS_FACT_COLUMNS]
    df["SKU_ID"] = pd.to_numeric(df["SKU_ID"], errors="coerce")
    df = df[df["SKU_ID"].notna()]
    df["SKU_ID"] = df["SKU_ID"].astype("int64")
    df["Branch_ID"] = df["Branch_ID"].astype(str)
    df["DateKey"] = df["DateKey"].astype("int32")
    for col in FLOAT_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
    # Early snapshots stored the raw ratio; the current definition caps it at 1.
    df["CoverageRatio"] = df["CoverageRatio"].clip(lower=0, upper=1)
    for col in BOOL_COLUMNS:
        df[col] = _as_bool(df[col])
    for col in TEXT_COLUMNS:
        df[col] = df[col].where(df[col].notna() & (df[col] != ""), None).astype("string")
    df = _dedupe_keys(df, ["SKU_ID", "Branch_ID", "DateKey"])
    return df.reset_index(drop=True)


def to_fact_branch_rop(wide: pd.DataFrame) -> pd.DataFrame:
    df = wide
    if "BranchName" in df:
        df = df.loc[~excluded_mask(df["BranchName"])]
    df = df.copy()
    for col in BRANCH_ROP_COLUMNS:
        if col not in df:
            df[col] = np.nan
    df = df[BRANCH_ROP_COLUMNS]
    df["SKU_ID"] = pd.to_numeric(df["SKU_ID"], errors="coerce").astype("int64")
    df["Branch_ID"] = df["Branch_ID"].astype(str)
    df["HasROP"] = _as_bool(df["HasROP"])
    # The VED the workbook calculated Z_VED and ROP with; VED itself may later
    # be replaced from the VED file (apply_sku_ved).
    df["VED_ROP"] = df["VED_ROP"].fillna(df["VED"])
    for col in ["ABC", "XYZ", "ABC_XYZ", "VED", "VED_ROP"]:
        df[col] = df[col].astype("string")
    df = _dedupe_keys(df, ["SKU_ID", "Branch_ID"])
    return df.reset_index(drop=True)


def apply_sku_ved(fact: pd.DataFrame, dim_sku: pd.DataFrame) -> pd.DataFrame:
    """Give the rows of every SKU listed in the VED file (dim_sku.VED_Source)
    that SKU's VED.

    Status facts also get their VED-dependent measures (WeightedGap,
    PriorityScore, IsCritical) recomputed on the rows that changed. ROP and
    Z_VED stay as the workbook calculated them; fact_branch_rop keeps the
    workbook's VED in VED_ROP."""
    if fact.empty or "VED_Source" not in dim_sku:
        return fact
    listed = dim_sku.loc[dim_sku["VED_Source"] == VED_FILE_SOURCE].set_index("SKU_ID")["VED"]
    if listed.empty:
        return fact
    new = fact["SKU_ID"].map(listed).astype(object)
    current = fact["VED"].astype(object).where(fact["VED"].notna(), None)
    changed = (new.notna() & (new != current)).to_numpy(dtype=bool)
    if not changed.any():
        return fact
    out = fact.copy()
    out.loc[changed, "VED"] = new[changed]
    if {"ROPGap", "Status", "WeightedGap", "PriorityScore", "IsCritical"}.issubset(out.columns):
        rows = out.loc[changed]
        confidence = dim_sku.set_index("SKU_ID")["ROP_Confidence"] if "ROP_Confidence" in dim_sku else pd.Series(dtype=object)
        low_confidence = rows["SKU_ID"].map(confidence).eq("Бага").fillna(False).astype(bool)
        measures = ved_measures(rows["VED"], rows["ROPGap"], rows["Status"], low_confidence)
        for col in measures:
            out.loc[changed, col] = measures[col]
    return out


def ved_differences(fact_branch_rop: pd.DataFrame, dim_sku: pd.DataFrame) -> pd.DataFrame:
    """SKUs whose VED now differs from the VED the ROP workbook used (VED_ROP),
    with the number of branch rows affected - the SKUs whose ROP the workbook
    calculated with another VED."""
    differs = fact_branch_rop["VED"].astype(object) != fact_branch_rop["VED_ROP"].astype(object)
    diff = fact_branch_rop.loc[differs]
    if diff.empty:
        return pd.DataFrame(columns=["Issue", "SourceName", "VED", "PreviousVED", "SKU_ID", "Rows"])
    out = diff.groupby("SKU_ID").agg(
        VED=("VED", "first"),
        PreviousVED=("VED_ROP", lambda s: s.mode().iloc[0]),
        Rows=("VED", "size"),
    ).reset_index()
    out["SourceName"] = out["SKU_ID"].map(dim_sku.set_index("SKU_ID")["SKU_Name"])
    out["Issue"] = "ROP файлын VED-ээс өөр — VED файлынхыг авав"
    return out[["Issue", "SourceName", "VED", "PreviousVED", "SKU_ID", "Rows"]]


def branch_rop_wide(fact_branch_rop: pd.DataFrame, dim_branch: pd.DataFrame, dim_sku: pd.DataFrame) -> pd.DataFrame:
    """Re-attach BranchName / Category so ``build_status_snapshot`` can run."""
    wide = fact_branch_rop.merge(dim_branch[["Branch_ID", "BranchName"]], on="Branch_ID", how="left")
    return wide.merge(dim_sku[["SKU_ID", "Category"]], on="SKU_ID", how="left")


def ensure_branches(dim_branch: pd.DataFrame, *frames: pd.DataFrame) -> pd.DataFrame:
    """Add any Branch_ID seen in a fact (e.g. UNMAPPED::...) to dim_branch."""
    dim = dim_branch[["BranchName", "Branch_ID"]].copy()
    known = set(dim["Branch_ID"].astype(str))
    extra = []
    for frame in frames:
        if frame is None or not {"Branch_ID", "BranchName"}.issubset(frame.columns):
            continue
        pairs = frame[["Branch_ID", "BranchName"]].dropna().astype(str).drop_duplicates("Branch_ID")
        new = pairs[~pairs["Branch_ID"].isin(known)]
        known.update(new["Branch_ID"])
        extra.append(new)
    extra = [frame for frame in extra if len(frame)]
    if extra:
        dim = pd.concat([dim, *extra], ignore_index=True)
    dim["Branch_ID"] = dim["Branch_ID"].astype(str)
    dim["BranchGroup"] = dim["BranchName"].map(branch_group)
    dim["IsNetwork"] = dim["Branch_ID"].eq("NETWORK")
    dim["IsMapped"] = ~dim["Branch_ID"].str.startswith("UNMAPPED::")
    dim["IsExcluded"] = dim["BranchName"].map(is_excluded_branch)
    return dim.reset_index(drop=True)


def build_dim_date(date_keys: list[int]) -> pd.DataFrame:
    dates = pd.to_datetime([str(k) for k in date_keys], format="%Y%m%d")
    start = pd.Timestamp(year=dates.min().year - 1, month=1, day=1)
    end = pd.Timestamp(year=dates.max().year + 1, month=12, day=31)
    df = pd.DataFrame({"Date": pd.date_range(start, end, freq="D")})
    df["DateKey"] = df["Date"].dt.strftime("%Y%m%d").astype("int32")
    df["Year"] = df["Date"].dt.year
    df["Quarter"] = "Q" + df["Date"].dt.quarter.astype(str)
    df["MonthNo"] = df["Date"].dt.month
    df["Month"] = df["Date"].dt.strftime("%Y-%m")
    df["MonthName"] = df["Date"].dt.month_name()
    df["Week"] = df["Date"].dt.isocalendar().week.astype(int)
    df["Day"] = df["Date"].dt.day
    df["HasSnapshot"] = df["DateKey"].isin(set(date_keys))
    return df


def build_static_dims(dim_sku: pd.DataFrame) -> dict[str, pd.DataFrame]:
    dim_status = pd.DataFrame({
        "Status": STATUS_ORDER,
        "StatusOrder": range(1, len(STATUS_ORDER) + 1),
        "StatusGroup": ["Эрсдэл", "Эрсдэл", "Өгөгдлийн чанар", "Өгөгдлийн чанар", "Хэвийн", "Илүүдэл"],
        "Severity": [3, 2, 1, 1, 0, 0],
    })
    dim_ved = pd.DataFrame({
        "VED": VED_ORDER,
        "VEDOrder": [1, 2, 3, 4],
        "ServiceLevel": [0.99, 0.96, 0.88, np.nan],
        "Z": [2.326, 1.751, 1.175, np.nan],
        "Criticality": ["Vital", "Essential", "Desirable", "Unknown"],
    })
    counts = dim_sku["Category"].value_counts()
    ordered = [c for c in counts.index if c != "Бусад"] + (["Бусад"] if "Бусад" in counts.index else [])
    dim_category = pd.DataFrame({"Category": ordered, "CategoryOrder": range(1, len(ordered) + 1)})
    dim_category["SKUCount"] = dim_category["Category"].map(counts).astype(int)
    return {"dim_status": dim_status, "dim_ved": dim_ved, "dim_category": dim_category}


DATA_DICTIONARY = pd.DataFrame([
    ("dim_sku", "SKU_ID", "SKU master key; Category comes from 'SKU category.csv' where matched"),
    ("dim_sku", "VED", "VED class; from 'VED ангилал.csv' where the SKU is listed (VED_Source = 'VED ангилал')"),
    ("dim_sku", "ROP_Used", "Network-level working ROP; corrected detail where available, fallback otherwise"),
    ("dim_branch", "Branch_ID", "Branch key; UNMAPPED::* for inventory branches missing from the ROP file"),
    ("dim_date", "DateKey", "yyyymmdd integer key; HasSnapshot marks dates with a status snapshot"),
    ("fact_branch_rop", "ROP", "Branch ROP (grain: SKU x branch)"),
    ("fact_branch_rop", "VED_ROP", "VED the ROP workbook used for Z_VED and ROP; VED is the current class"),
    ("fact_branch_rop", "ABC / XYZ", "Branch ABC (sales value) and XYZ (demand variability) class from the ROP workbook"),
    ("fact_branch_rop", "AvgMonthlyDemand_Branch", "Average monthly sales; the dashboard leaves out SKU x branch rows where it is missing or 0"),
    ("fact_abc_xyz_file", "ABC / XYZ", "Branch class from 'ABC XYZ.xlsx'; fills the ROP workbook's missing classes in the ABC / XYZ tab"),
    ("fact_sku_status", "InventoryPosition", "OnHand + OnOrder - Backorder (grain: SKU x branch x date)"),
    ("fact_sku_status", "ROPGap", "MAX(ROP - InventoryPosition, 0)"),
    ("fact_sku_status", "Status", "Stockout / below ROP / normal / excess / missing data / missing ROP"),
    ("fact_sku_status", "WeightedGap", "ROPGap weighted by VED criticality"),
    ("fact_sku_status", "PriorityScore", "Operational prioritization score"),
    ("fact_status_history", "*", "Same columns as fact_sku_status, one set of rows per snapshot DateKey"),
    ("fact_batch_expiry", "ExpiryDateKey", "Batch expiry yyyymmdd from the export's expiry column; NULL = unknown (grain: SKU x branch x expiry x date)"),
    ("fact_batch_expiry", "Qty", "Stock of the batch; SUM(Qty) per SKU x branch x date = fact_sku_status.OnHand"),
    ("fact_batch_expiry", "DTE", "Days to expiry: ExpiryDate - snapshot date"),
    ("fact_batch_expiry", "ExpiryStatus", "EXPIRED / CRITICAL / WARNING / WATCH / OK / UNKNOWN; thresholds in expiry_config.json"),
    ("fact_batch_expiry", "PullFlag", "0 <= DTE < MinDispenseDays: off the dispensing shelf, not usable stock"),
    ("fact_batch_expiry", "FEFORank", "Pick order of the usable batches of a SKU x branch; 1 = earliest expiry"),
    ("fact_batch_expiry", "ProjectedWaste", "Qty demand will not use before DTE - MinDispenseDays (FEFO walk); NULL without a demand figure"),
    ("fact_batch_expiry", "AtRisk", "ProjectedWaste > 0"),
    ("fact_batch_expiry_history", "*", "Same columns as fact_batch_expiry, one set of rows per snapshot DateKey"),
], columns=["Table", "Field", "Definition"])


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
def _powerbi_frame(name: str, frame: pd.DataFrame, dims: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Power BI users get readable columns next to the keys."""
    if name not in {"fact_sku_status", "fact_status_history", "fact_branch_rop"}:
        return frame
    out = frame
    if "dim_sku" in dims:
        out = out.merge(dims["dim_sku"][["SKU_ID", "SKU_Name", "Category"]], on="SKU_ID", how="left")
    if "dim_branch" in dims:
        out = out.merge(dims["dim_branch"][["Branch_ID", "BranchName"]], on="Branch_ID", how="left")
    return out


def _stringify_objects(frame: pd.DataFrame) -> pd.DataFrame:
    """Mixed-type object columns (e.g. codes read from Excel) can't go to Parquet."""
    objects = frame.select_dtypes(include="object").columns
    if len(objects) == 0:
        return frame
    return frame.astype({col: "string" for col in objects})


def write_tables(
    frames: dict[str, pd.DataFrame],
    data_dir: Path = DATA_DIR,
    powerbi: bool = False,
    refresh_db: bool = True,
) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    for name, frame in frames.items():
        _stringify_objects(frame).to_parquet(data_dir / f"{name}.parquet", index=False)
    if powerbi:
        POWERBI_DIR.mkdir(parents=True, exist_ok=True)
        dims = load_project_data(data_dir)
        for name, frame in frames.items():
            _powerbi_frame(name, frame, dims).to_csv(POWERBI_DIR / f"{name}.csv", index=False, encoding="utf-8-sig")
    if refresh_db:
        build_duckdb_file(data_dir)


def connect(data_dir: Path = DATA_DIR, materialize: bool = True) -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB connection with every warehouse table loaded.

    The dashboard uses this rather than warehouse.duckdb so the ETL scripts
    can rewrite the files while the app is running."""
    con = duckdb.connect()
    kind = "TABLE" if materialize else "VIEW"
    for name in TABLES:
        path = data_dir / f"{name}.parquet"
        if path.exists():
            con.execute(f"CREATE {kind} {name} AS SELECT * FROM read_parquet('{_sql_path(path)}')")
    return con


def _sql_path(path: Path) -> str:
    return str(path).replace("\\", "/").replace("'", "''")


def build_duckdb_file(data_dir: Path = DATA_DIR) -> Path | None:
    """Write data/warehouse.duckdb for external SQL tools (DBeaver, Power BI ODBC)."""
    db_path = data_dir / "warehouse.duckdb"
    tmp_path = data_dir / "warehouse.duckdb.tmp"
    tmp_path.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp_path))
    try:
        for name in TABLES:
            path = data_dir / f"{name}.parquet"
            if path.exists():
                con.execute(f"CREATE TABLE {name} AS SELECT * FROM read_parquet('{_sql_path(path)}')")
    finally:
        con.close()
    try:
        tmp_path.replace(db_path)
    except PermissionError:
        print(f"warehouse.duckdb is open in another program; left the new copy at {tmp_path.name}")
        return None
    return db_path


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------
def read_snapshot_history(snapshot_dir: Path = SNAPSHOT_DIR) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load every snapshots/status_*.csv.gz into the history fact.

    The date comes from the file's SnapshotDate column, not its name; when
    two files carry the same date the most recently written one wins.
    Returns (history fact, Branch_ID/BranchName pairs seen)."""
    by_date: dict[int, pd.DataFrame] = {}
    branches = []
    files = sorted(snapshot_dir.glob("status_*.csv.gz"), key=lambda p: p.stat().st_mtime)
    for path in files:
        wide = pd.read_csv(path, low_memory=False)
        if wide.empty:
            continue
        branches.append(wide[["Branch_ID", "BranchName"]].drop_duplicates())
        fact = to_fact_status(wide)
        for key, part in fact.groupby("DateKey"):
            by_date[int(key)] = part
    history = pd.concat(by_date.values(), ignore_index=True) if by_date else to_fact_status(pd.DataFrame(columns=STATUS_FACT_COLUMNS))
    pairs = pd.concat(branches, ignore_index=True) if branches else pd.DataFrame(columns=["Branch_ID", "BranchName"])
    return history.sort_values(["DateKey", "SKU_ID", "Branch_ID"]).reset_index(drop=True), pairs


def upsert_history(history: pd.DataFrame | None, fact_status: pd.DataFrame) -> pd.DataFrame:
    """Replace the rows for the dates in ``fact_status`` and append them."""
    if history is None or history.empty:
        return fact_status.reset_index(drop=True)
    keys = set(fact_status["DateKey"].unique())
    kept = history[~history["DateKey"].isin(keys)]
    return pd.concat([kept, fact_status], ignore_index=True).sort_values(["DateKey", "SKU_ID", "Branch_ID"]).reset_index(drop=True)


def status_tables(status_wide: pd.DataFrame, dim_branch: pd.DataFrame, *branch_sources: pd.DataFrame,
                  data_dir: Path = DATA_DIR) -> dict[str, pd.DataFrame]:
    """Tables to write after a new status snapshot: the current fact, the
    history with this date replaced, and dimensions that cover every key."""
    fact = to_fact_status(status_wide)
    history_path = data_dir / "fact_status_history.parquet"
    history = pd.read_parquet(history_path) if history_path.exists() else None
    history = upsert_history(history, fact)
    return {
        "fact_sku_status": fact,
        "fact_status_history": history,
        "dim_branch": ensure_branches(dim_branch, status_wide, *branch_sources),
        "dim_date": build_dim_date(sorted(set(history["DateKey"].astype(int)))),
    }


def save_snapshot_files(inventory: pd.DataFrame, status_wide: pd.DataFrame, review: pd.DataFrame, snapshot_date: Any,
                        snapshot_dir: Path = SNAPSHOT_DIR) -> None:
    snapshot_dir.mkdir(exist_ok=True)
    stamp = pd.Timestamp(snapshot_date).strftime("%Y%m%d")
    fast_gzip = {"method": "gzip", "compresslevel": 1}
    inventory.to_csv(snapshot_dir / f"inventory_{stamp}.csv.gz", index=False, encoding="utf-8-sig", compression=fast_gzip)
    status_wide.to_csv(snapshot_dir / f"status_{stamp}.csv.gz", index=False, encoding="utf-8-sig", compression=fast_gzip)
    review.to_csv(snapshot_dir / f"mapping_review_{stamp}.csv", index=False, encoding="utf-8-sig")


# --------------------------------------------------------------------------
# Integrity
# --------------------------------------------------------------------------
def _batch_expiry_checks(con: duckdb.DuckDBPyConnection, tables: set[str]) -> dict[str, Any]:
    """fact_batch_expiry must describe the same snapshot as fact_sku_status,
    and its batches must add up to each SKU x branch's OnHand."""
    q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    c: dict[str, Any] = {}
    c["fact_batch_expiry_rows"] = q("SELECT count(*) FROM fact_batch_expiry")
    c["fact_batch_expiry_unique_key"] = q(
        "SELECT count(*) = count(DISTINCT (SKU_ID, Branch_ID, ExpiryDateKey, DateKey)) FROM fact_batch_expiry")
    c["fact_batch_expiry_orphan_sku"] = q("SELECT count(*) FROM fact_batch_expiry ANTI JOIN dim_sku USING (SKU_ID)")
    c["fact_batch_expiry_orphan_branch"] = q("SELECT count(*) FROM fact_batch_expiry ANTI JOIN dim_branch USING (Branch_ID)")
    c["fact_batch_expiry_excluded_branch_rows"] = q(
        "SELECT count(*) FROM fact_batch_expiry JOIN dim_branch USING (Branch_ID) WHERE IsExcluded")
    c["fact_batch_expiry_invalid_fefo"] = q(
        "SELECT count(*) FROM fact_batch_expiry WHERE (FEFORank IS NOT NULL) <> IsUsable "
        "OR ProjectedWaste < -1e-9 OR ProjectedWaste > Qty + 1e-9")
    if "fact_sku_status" in tables:
        batch_dates = [r[0] for r in con.execute("SELECT DISTINCT DateKey FROM fact_batch_expiry ORDER BY 1").fetchall()]
        status_dates = [r[0] for r in con.execute("SELECT DISTINCT DateKey FROM fact_sku_status ORDER BY 1").fetchall()]
        # A stale batch fact (built from an earlier snapshot) fails here;
        # reconciling it row by row would only repeat that.
        c["fact_batch_expiry_invalid_snapshot"] = int(batch_dates != status_dates)
        if batch_dates == status_dates:
            c["fact_batch_expiry_invalid_reconciliation"] = q("""
                WITH b AS (SELECT SKU_ID, Branch_ID, DateKey, sum(Qty) AS qty FROM fact_batch_expiry GROUP BY ALL),
                     s AS (SELECT SKU_ID, Branch_ID, DateKey, coalesce(OnHand, 0) AS qty FROM fact_sku_status)
                SELECT count(*) FROM b FULL JOIN s USING (SKU_ID, Branch_ID, DateKey)
                WHERE abs(coalesce(b.qty, 0) - coalesce(s.qty, 0)) > 1e-6 * greatest(1, abs(coalesce(s.qty, 0)))
            """)
    return c


def integrity_checks(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    c: dict[str, Any] = {}
    c["dim_sku_rows"] = q("SELECT count(*) FROM dim_sku")
    c["dim_sku_unique_id"] = q("SELECT count(*) = count(DISTINCT SKU_ID) FROM dim_sku")
    c["dim_branch_rows"] = q("SELECT count(*) FROM dim_branch")
    c["dim_branch_unique_id"] = q("SELECT count(*) = count(DISTINCT Branch_ID) FROM dim_branch")
    c["dim_date_unique_key"] = q("SELECT count(*) = count(DISTINCT DateKey) FROM dim_date")

    def fk(table: str, col: str, dim: str) -> int:
        return q(f"SELECT count(*) FROM {table} f ANTI JOIN {dim} d USING ({col})")

    c["fact_branch_rop_rows"] = q("SELECT count(*) FROM fact_branch_rop")
    c["fact_branch_rop_unique_key"] = q("SELECT count(*) = count(DISTINCT (SKU_ID, Branch_ID)) FROM fact_branch_rop")
    c["fact_branch_rop_orphan_sku"] = fk("fact_branch_rop", "SKU_ID", "dim_sku")
    c["fact_branch_rop_orphan_branch"] = fk("fact_branch_rop", "Branch_ID", "dim_branch")
    for table in ["fact_sku_status", "fact_status_history"]:
        if table not in tables:
            c[f"{table}_rows"] = 0
            continue
        c[f"{table}_rows"] = q(f"SELECT count(*) FROM {table}")
        c[f"{table}_unique_key"] = q(f"SELECT count(*) = count(DISTINCT (SKU_ID, Branch_ID, DateKey)) FROM {table}")
        c[f"{table}_orphan_sku"] = fk(table, "SKU_ID", "dim_sku")
        c[f"{table}_orphan_branch"] = fk(table, "Branch_ID", "dim_branch")
        c[f"{table}_orphan_date"] = fk(table, "DateKey", "dim_date")
        c[f"{table}_negative_gap"] = q(f"SELECT count(*) FROM {table} WHERE ROPGap < 0")
        c[f"{table}_invalid_coverage"] = q(f"SELECT count(*) FROM {table} WHERE CoverageRatio < 0 OR CoverageRatio > 1")
        c[f"{table}_excluded_branch_rows"] = q(f"SELECT count(*) FROM {table} JOIN dim_branch USING (Branch_ID) WHERE IsExcluded")
    # VED: a known class everywhere, and every SKU listed in the VED file
    # carries that file's VED in each fact (one pipeline skipping
    # apply_sku_ved would show up here).
    known_ved = ", ".join(f"'{v}'" for v in VED_ORDER)
    has_source = q("SELECT count(*) FROM information_schema.columns WHERE table_name = 'dim_sku' AND column_name = 'VED_Source'")
    for table in ["fact_branch_rop", "fact_sku_status", "fact_status_history"]:
        if table not in tables:
            continue
        listed = f" OR (s.VED_Source = '{VED_FILE_SOURCE}' AND f.VED IS DISTINCT FROM s.VED)" if has_source else ""
        c[f"{table}_invalid_ved"] = q(
            f"SELECT count(*) FROM {table} f LEFT JOIN dim_sku s USING (SKU_ID) "
            f"WHERE f.VED IS NULL OR f.VED NOT IN ({known_ved}){listed}"
        )
    if "fact_batch_expiry" in tables:
        c.update(_batch_expiry_checks(con, tables))
    if "fact_status_history" in tables:
        c["history_dates"] = [str(r[0]) for r in con.execute("SELECT DISTINCT DateKey FROM fact_status_history ORDER BY 1").fetchall()]
    c["negative_rop_rows"] = q("SELECT count(*) FROM fact_branch_rop WHERE ROP < 0")
    c["alias_unique_key"] = q("SELECT count(*) = count(DISTINCT NormalizedKey) FROM alias_mapping")
    review_path = DATA_DIR / "category_review.csv"
    if review_path.exists():  # informational: SKUs that kept their old category
        review = pd.read_csv(review_path)
        c["category_uncovered_sku"] = int((review["SKU_ID"].notna() & review["MatchType"].isna()).sum())

    must_be_true = [k for k in c if k.endswith("_unique_id") or k.endswith("_unique_key")]
    must_be_zero = [k for k in c if "orphan" in k or "negative" in k or "invalid" in k or "excluded_branch" in k]
    c["failed"] = [k for k in must_be_true if not c[k]] + [k for k in must_be_zero if c[k] != 0]
    c["all_ok"] = not c["failed"]
    return c


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------
def build(categories: Path | None = None, powerbi: bool = False, data_dir: Path = DATA_DIR,
          ved: Path | None = None) -> dict[str, Any]:
    frames = load_project_data(data_dir)
    metadata_path = data_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}

    dim_sku = frames["dim_sku"].copy()
    dim_sku["SKU_ID"] = pd.to_numeric(dim_sku["SKU_ID"], errors="coerce").astype("int64")
    dim_sku["Category"] = dim_sku["Category"].map(clean_category)
    alias = frames["alias_mapping"].copy()
    alias["SKU_ID"] = pd.to_numeric(alias["SKU_ID"], errors="coerce").astype("int64")
    alias["NormalizedKey"] = alias["NormalizedKey"].astype(str)
    alias = alias.drop_duplicates("NormalizedKey", keep="last").reset_index(drop=True)

    if categories is not None:
        category_by_sku, review, cat_stats = load_category_map(categories, dim_sku, alias)
        dim_sku = apply_category_map(dim_sku, category_by_sku)
        review.to_csv(data_dir / "category_review.csv", index=False, encoding="utf-8-sig")
        metadata["categories"] = {"source_file": Path(categories).name, **cat_stats}
    if ved is not None:
        ved_by_sku, ved_review, ved_stats = load_ved_map(ved, dim_sku, alias)
        before = dim_sku.set_index("SKU_ID")["VED"]
        dim_sku = apply_ved_map(dim_sku, ved_by_sku)
        ved_stats["dim_sku_changed"] = int((dim_sku.set_index("SKU_ID")["VED"] != before).sum())
        metadata["ved"] = {"source_file": Path(ved).name, **ved_stats}
    for col in ["HasROP", "HasDetailROP"]:
        if col in dim_sku:
            dim_sku[col] = _as_bool(dim_sku[col])

    raw_rop = frames["fact_branch_rop"]
    raw_status = frames["fact_sku_status"]
    inventory = frames.get("fact_inventory_snapshot", pd.DataFrame()).copy()
    history, history_branches = read_snapshot_history()
    dim_branch = ensure_branches(frames["dim_branch"], raw_rop, raw_status, inventory, history_branches)

    # Snapshot files keep the VED of their day, so the VED file is applied
    # again to every fact on every build.
    fact_branch_rop = apply_sku_ved(to_fact_branch_rop(raw_rop), dim_sku)
    fact_status = apply_sku_ved(to_fact_status(raw_status, metadata.get("snapshot_date")), dim_sku)
    history = apply_sku_ved(upsert_history(history, fact_status), dim_sku)

    if ved is not None:
        differences = ved_differences(fact_branch_rop, dim_sku)
        pd.concat([ved_review, differences], ignore_index=True).to_csv(
            data_dir / "ved_review.csv", index=False, encoding="utf-8-sig")
        metadata["ved"].update({
            "sku_differs_from_rop_workbook": int(len(differences)),
            "branch_rows_differ_from_rop_workbook": int(differences["Rows"].sum()) if len(differences) else 0,
        })

    if len(inventory):
        inventory["SKU_ID"] = pd.to_numeric(inventory["SKU_ID"], errors="coerce").astype("Int64")
        inventory["DateKey"] = pd.to_datetime(inventory["SnapshotDate"]).dt.strftime("%Y%m%d").astype("int32")

    date_keys = sorted(set(history["DateKey"].astype(int)))
    out = {
        "dim_sku": dim_sku,
        "dim_branch": dim_branch,
        "dim_date": build_dim_date(date_keys),
        **build_static_dims(dim_sku),
        "fact_branch_rop": fact_branch_rop,
        "fact_sku_status": fact_status,
        "fact_status_history": history,
        "fact_inventory_snapshot": inventory,
        "alias_mapping": alias,
        "mapping_review": frames.get("mapping_review", pd.DataFrame()),
        "data_dictionary": DATA_DICTIONARY,
    }
    write_tables(out, data_dir=data_dir, powerbi=powerbi)

    metadata["warehouse"] = {name: int(len(frame)) for name, frame in out.items()}
    metadata["history_dates"] = [pd.Timestamp(str(k)).date().isoformat() for k in date_keys]
    metadata["branch_rop_rows"] = int(len(fact_branch_rop))
    metadata["corrected_branch_rop_total"] = float(fact_branch_rop["ROP"].sum())
    save_json(metadata_path, metadata)

    con = connect(data_dir)
    checks = integrity_checks(con)
    con.close()
    save_json(BASE_DIR / "QA_REPORT.json", checks)
    return checks


def rebuild_history(data_dir: Path = DATA_DIR) -> dict[str, Any]:
    frames = load_project_data(data_dir)
    history, pairs = read_snapshot_history()
    history = upsert_history(history, to_fact_status(frames["fact_sku_status"]))
    history = apply_sku_ved(history, frames["dim_sku"])
    dim_branch = ensure_branches(frames["dim_branch"], pairs)
    date_keys = sorted(set(history["DateKey"].astype(int)))
    write_tables({"fact_status_history": history, "dim_branch": dim_branch, "dim_date": build_dim_date(date_keys)}, data_dir)
    con = connect(data_dir)
    checks = integrity_checks(con)
    con.close()
    return checks


def main() -> None:
    parser = argparse.ArgumentParser(description="Build / check the ROP star-schema warehouse.")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="Rebuild all Parquet tables and warehouse.duckdb")
    b.add_argument("--categories", type=Path, default=None, help="CSV: product name, category")
    b.add_argument("--ved", type=Path, default=None, help="CSV: product name, ..., VED (V / E / D)")
    b.add_argument("--powerbi", action="store_true", help="Also export readable CSVs to powerbi_data/")
    sub.add_parser("history", help="Rebuild fact_status_history from snapshots/")
    sub.add_parser("check", help="Run integrity checks only")
    args = parser.parse_args()

    if args.command == "build":
        checks = build(args.categories, args.powerbi, ved=args.ved)
    elif args.command == "history":
        checks = rebuild_history()
    else:
        con = connect()
        checks = integrity_checks(con)
        con.close()
    print(json.dumps(checks, ensure_ascii=False, indent=2, default=str))
    if not checks["all_ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
