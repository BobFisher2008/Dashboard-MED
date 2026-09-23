"""Batch expiry: expiry status, FEFO rank and sell-through projection per batch.

One row per SKU x branch x expiry date for the latest snapshot, built from
the inventory rows of the last refresh (``fact_inventory_snapshot``), which
keep the export's expiry column (``Сери.Хүртэл хүчинтэй``).

    python expiry.py [--config expiry_config.json]

Rules (thresholds come from expiry_config.json, see ExpiryConfig):

    DTE       ExpiryDate - SnapshotDate, in days
    Status    UNKNOWN (no valid date) / EXPIRED (DTE < 0) / CRITICAL / WARNING / WATCH / OK
    Pull      0 <= DTE < MinDispenseDays: off the dispensing shelf
    Usable    Qty > 0 and DTE >= MinDispenseDays
    FEFO      usable batches of a SKU x branch ranked by expiry; 1 = pick next
    Sold      what branch demand takes from each batch before its sell-by day
              (DTE - MinDispenseDays), earliest expiry first
    Waste     Qty - Sold; expired and pulled stock is all waste. AtRisk = Waste > 0

Demand is the ROP workbook's AvgMonthlyDemand_Branch. A SKU x branch the
workbook does not list (reserve / tender warehouses, branches missing from
it) has no demand figure: its batches get a FEFO rank but no projection,
rather than being counted as waste. A listed pair with zero demand is
projected as all waste.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from common import excluded_mask, save_json
from warehouse import DATA_DIR, upsert_history, write_tables

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "expiry_config.json"

# Most urgent first.
EXPIRY_STATUS_ORDER = ["EXPIRED", "CRITICAL", "WARNING", "WATCH", "OK", "UNKNOWN"]
EXPIRY_STATUS_COLORS = {
    "EXPIRED": "#7F1D1D", "CRITICAL": "#DC2626", "WARNING": "#F97316",
    "WATCH": "#FACC15", "OK": "#16A34A", "UNKNOWN": "#9CA3AF",
}

BATCH_COLUMNS = [
    # keys: the batch (SKU x branch x expiry date) and the snapshot
    "SKU_ID", "Branch_ID", "ExpiryDateKey", "DateKey",
    # measures
    "Qty", "InventoryLines", "DTE", "MinDispenseDays", "FEFORank", "DailyDemand",
    "ProjectedSold", "ProjectedWaste", "UnitCost", "StockValue", "ProjectedWasteValue",
    # degenerate attributes of the batch grain
    "ExpiryDate", "ExpiryStatus", "PullFlag", "IsUsable", "HasDemandData", "AtRisk",
    "FirstSeenDateKey", "ConfigVersion",
]


@dataclass(frozen=True)
class ExpiryConfig:
    """Expiry thresholds, in days. Loaded from expiry_config.json."""

    critical_days: int = 30
    warning_days: int = 90
    watch_days: int = 180
    # Stock with less life left than this comes off the dispensing shelf.
    min_dispense_days: int = 30
    min_dispense_days_by_category: dict[str, int] = field(default_factory=dict)
    min_dispense_days_by_sku: dict[int, int] = field(default_factory=dict)
    # An expiry further ahead than this (or before 2000) is a data error: UNKNOWN.
    max_years_ahead: int = 10
    days_per_month: float = 30.4

    def __post_init__(self) -> None:
        if not 0 <= self.critical_days <= self.warning_days <= self.watch_days:
            raise ValueError("expiry config: need 0 <= critical_days <= warning_days <= watch_days")
        overrides = [*self.min_dispense_days_by_category.values(), *self.min_dispense_days_by_sku.values()]
        if min([self.min_dispense_days, *overrides]) < 0:
            raise ValueError("expiry config: min_dispense_days cannot be negative")
        if self.max_years_ahead <= 0 or self.days_per_month <= 0:
            raise ValueError("expiry config: max_years_ahead and days_per_month must be positive")

    @property
    def version(self) -> str:
        """Short hash of the settings; stored on every row built with them."""
        blob = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]

    @classmethod
    def load(cls, path: str | Path | None = CONFIG_PATH) -> ExpiryConfig:
        """Defaults, overridden by the JSON file when it exists.
        Keys starting with "_" are comments."""
        if path is None or not Path(path).exists():
            return cls()
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        raw = {k: v for k, v in raw.items() if not k.startswith("_")}
        unknown = sorted(set(raw) - {f.name for f in fields(cls)})
        if unknown:
            raise ValueError(f"{Path(path).name}: unknown setting(s) {unknown}")
        if "min_dispense_days_by_sku" in raw:
            raw["min_dispense_days_by_sku"] = {int(k): int(v) for k, v in raw["min_dispense_days_by_sku"].items()}
        return cls(**raw)


def build_dim_expiry_status(cfg: ExpiryConfig) -> pd.DataFrame:
    """Display order, label, day range and colour of each ExpiryStatus."""
    c, w, h = cfg.critical_days, cfg.warning_days, cfg.watch_days
    rows = [
        ("EXPIRED", "Хугацаа дууссан", None, -1),
        ("CRITICAL", f"0–{c} хоног", 0, c),
        ("WARNING", f"{c + 1}–{w} хоног", c + 1, w),
        ("WATCH", f"{w + 1}–{h} хоног", w + 1, h),
        ("OK", f"{h + 1}+ хоног", h + 1, None),
        ("UNKNOWN", "Хугацаа тодорхойгүй", None, None),
    ]
    dim = pd.DataFrame(rows, columns=["ExpiryStatus", "StatusLabel", "MinDays", "MaxDays"])
    dim["StatusOrder"] = range(1, len(dim) + 1)
    dim["Color"] = dim["ExpiryStatus"].map(EXPIRY_STATUS_COLORS)
    dim[["MinDays", "MaxDays"]] = dim[["MinDays", "MaxDays"]].astype("Int32")
    return dim


# --------------------------------------------------------------------------
# Building the fact
# --------------------------------------------------------------------------
def implausible_expiry(expiry: pd.Series, snapshot_date: Any, cfg: ExpiryConfig) -> pd.Series:
    snap = pd.Timestamp(snapshot_date).normalize()
    return (expiry < pd.Timestamp("2000-01-01")) | (expiry > snap + pd.DateOffset(years=cfg.max_years_ahead))


def _min_dispense_days(sku: pd.Series, dim_sku: pd.DataFrame, cfg: ExpiryConfig) -> pd.Series:
    days = pd.Series(float(cfg.min_dispense_days), index=sku.index)
    if cfg.min_dispense_days_by_category and "Category" in dim_sku:
        category = sku.map(dim_sku.drop_duplicates("SKU_ID").set_index("SKU_ID")["Category"])
        days = category.map(cfg.min_dispense_days_by_category).astype("float64").fillna(days)
    if cfg.min_dispense_days_by_sku:
        days = sku.map(cfg.min_dispense_days_by_sku).astype("float64").fillna(days)
    return days.astype("int32")


def _monthly_demand(batches: pd.DataFrame, dim_sku: pd.DataFrame, fact_branch_rop: pd.DataFrame) -> pd.Series:
    """AvgMonthlyDemand_Branch per SKU x branch; the SKU's network demand for
    NETWORK rows. NaN where the ROP workbook has no figure; negative = 0."""
    demand = pd.Series(np.nan, index=batches.index)
    if len(fact_branch_rop) and "AvgMonthlyDemand_Branch" in fact_branch_rop:
        rop = fact_branch_rop[["SKU_ID", "Branch_ID", "AvgMonthlyDemand_Branch"]].copy()
        rop["SKU_ID"] = pd.to_numeric(rop["SKU_ID"], errors="coerce").astype("Int64")
        rop = rop.drop_duplicates(["SKU_ID", "Branch_ID"])
        key = pd.MultiIndex.from_frame(batches[["SKU_ID", "Branch_ID"]].astype({"SKU_ID": "Int64"}))
        demand[:] = rop.set_index(["SKU_ID", "Branch_ID"])["AvgMonthlyDemand_Branch"].reindex(key).to_numpy()
    network = batches["Branch_ID"].eq("NETWORK")
    if network.any() and "AvgMonthlyDemand" in dim_sku:
        per_sku = dim_sku.drop_duplicates("SKU_ID").set_index("SKU_ID")["AvgMonthlyDemand"]
        demand[network] = batches.loc[network, "SKU_ID"].map(per_sku)
    return pd.to_numeric(demand, errors="coerce").clip(lower=0)


def project_sell_through(batches: pd.DataFrame) -> pd.DataFrame:
    """FEFORank and ProjectedSold for the usable rows of ``batches``.

    Walking a SKU x branch's batches earliest expiry first, batch i can sell
    only until its sell-by day S_i = DTE_i - MinDispenseDays, and only to
    demand the earlier batches have not already taken:

        Sold_i = min(Q_i, max(0, d * S_i - sum(Sold_j for j < i)))

    With D_i = d * S_i non-decreasing along the walk, the cumulative sold
    C_i = min(C_{i-1} + Q_i, D_i) has the closed form
    C_i = cumQ_i + min(0, min_{k<=i}(D_k - cumQ_k)), so the walk runs as
    grouped cumsum / cummin instead of a Python loop. Rows without a demand
    figure (DailyDemand NaN) get a rank and a NaN ProjectedSold."""
    keys = ["SKU_ID", "Branch_ID"]
    u = batches.loc[batches["IsUsable"], keys + ["ExpiryDateKey", "Qty", "DTE", "MinDispenseDays", "DailyDemand"]]
    u = u.sort_values(keys + ["ExpiryDateKey"], kind="stable")
    by = [u["SKU_ID"], u["Branch_ID"]]
    demand_to_sell_by = u["DailyDemand"] * (u["DTE"].astype("float64") - u["MinDispenseDays"])
    cum_qty = u["Qty"].groupby(by, sort=False).cumsum()
    slack = (demand_to_sell_by - cum_qty).groupby(by, sort=False).cummin()
    cum_sold = cum_qty + np.minimum(slack, 0.0)
    sold = cum_sold - cum_sold.groupby(by, sort=False).shift(fill_value=0.0)
    return pd.DataFrame({
        "FEFORank": u.groupby(keys, sort=False).cumcount() + 1,
        "ProjectedSold": np.minimum(sold.clip(lower=0.0), u["Qty"]),
    }, index=u.index)


def build_batch_expiry(
    inventory: pd.DataFrame,
    dim_sku: pd.DataFrame,
    fact_branch_rop: pd.DataFrame,
    snapshot_date: Any,
    cfg: ExpiryConfig | None = None,
    history: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """fact_batch_expiry for one snapshot.

    Same rows as fact_sku_status sums (matched SKUs, excluded branches left
    out), so sum(Qty) per SKU x branch equals its OnHand. ``history`` is the
    batch history so far; it only feeds FirstSeenDateKey."""
    cfg = cfg or ExpiryConfig()
    snap = pd.Timestamp(snapshot_date).normalize()
    date_key = int(snap.strftime("%Y%m%d"))

    rows = inventory.loc[inventory["SKU_ID"].notna() & ~excluded_mask(inventory["BranchName"])]
    expiry = pd.to_datetime(rows["ExpiryDate"], errors="coerce") if "ExpiryDate" in rows else pd.Series(pd.NaT, index=rows.index)
    expiry = expiry.dt.normalize().mask(implausible_expiry(expiry, snap, cfg))
    rows = pd.DataFrame({
        "SKU_ID": pd.to_numeric(rows["SKU_ID"]).astype("int64"),
        "Branch_ID": rows["Branch_ID"].astype(str),
        "_ekey": (expiry.dt.year * 10000 + expiry.dt.month * 100 + expiry.dt.day).fillna(0).astype("int64"),
        "OnHand": pd.to_numeric(rows["OnHand"], errors="coerce").fillna(0.0),
    })
    b = rows.groupby(["SKU_ID", "Branch_ID", "_ekey"], as_index=False).agg(
        Qty=("OnHand", "sum"), InventoryLines=("OnHand", "size"))

    b["ExpiryDateKey"] = b["_ekey"].where(b["_ekey"] > 0).astype("Int32")
    b["ExpiryDate"] = pd.to_datetime(b["ExpiryDateKey"].astype("string"), format="%Y%m%d", errors="coerce")
    b["DateKey"] = pd.Series(date_key, index=b.index, dtype="int32")
    dte = (b["ExpiryDate"] - snap).dt.days
    known = dte.notna()
    b["DTE"] = dte.astype("Int32")
    b["ExpiryStatus"] = np.select(
        [~known, dte < 0, dte <= cfg.critical_days, dte <= cfg.warning_days, dte <= cfg.watch_days],
        ["UNKNOWN", "EXPIRED", "CRITICAL", "WARNING", "WATCH"], default="OK")

    b["MinDispenseDays"] = _min_dispense_days(b["SKU_ID"], dim_sku, cfg)
    positive = b["Qty"] > 0
    b["PullFlag"] = positive & known & (dte >= 0) & (dte < b["MinDispenseDays"])
    b["IsUsable"] = positive & known & (dte >= b["MinDispenseDays"])

    monthly = _monthly_demand(b, dim_sku, fact_branch_rop)
    b["DailyDemand"] = monthly / cfg.days_per_month
    b["HasDemandData"] = monthly.notna()

    projected = project_sell_through(b)
    b["FEFORank"] = projected["FEFORank"].reindex(b.index).astype("Int32")
    sold = projected["ProjectedSold"].reindex(b.index)
    unsellable = positive & (b["ExpiryStatus"].eq("EXPIRED") | b["PullFlag"])
    sold[unsellable] = 0.0
    b["ProjectedSold"] = sold  # NaN: no valid date, no stock, or no demand figure
    b["ProjectedWaste"] = b["Qty"] - sold
    b["AtRisk"] = b["ProjectedWaste"].fillna(0) > 0

    cost = dim_sku.drop_duplicates("SKU_ID").set_index("SKU_ID")["UnitCost"] if "UnitCost" in dim_sku else None
    b["UnitCost"] = pd.to_numeric(b["SKU_ID"].map(cost), errors="coerce") if cost is not None else np.nan
    b["StockValue"] = b["Qty"] * b["UnitCost"]
    b["ProjectedWasteValue"] = b["ProjectedWaste"] * b["UnitCost"]

    seen = b[["SKU_ID", "Branch_ID", "_ekey", "DateKey"]]
    if history is not None and len(history):
        prior = history.loc[history["DateKey"] < date_key, ["SKU_ID", "Branch_ID", "ExpiryDateKey", "DateKey"]]
        prior = prior.assign(_ekey=prior["ExpiryDateKey"].fillna(0).astype("int64")).drop(columns="ExpiryDateKey")
        seen = pd.concat([prior, seen], ignore_index=True)
    first = seen.groupby(["SKU_ID", "Branch_ID", "_ekey"])["DateKey"].min().rename("FirstSeenDateKey")
    b = b.join(first, on=["SKU_ID", "Branch_ID", "_ekey"])
    b["FirstSeenDateKey"] = b["FirstSeenDateKey"].astype("int32")
    b["ConfigVersion"] = cfg.version

    return b[BATCH_COLUMNS].sort_values(["SKU_ID", "Branch_ID", "ExpiryDateKey"]).reset_index(drop=True)


def summarize(batches: pd.DataFrame) -> dict[str, Any]:
    """Headline numbers of one fact_batch_expiry (run metadata, metadata.json)."""
    stocked = batches[batches["Qty"] > 0]
    lines = batches["InventoryLines"].sum()
    dated_lines = batches.loc[batches["ExpiryStatus"] != "UNKNOWN", "InventoryLines"].sum()
    by_status = stocked.groupby("ExpiryStatus")["Qty"].agg(["size", "sum"])
    return {
        "batch_rows": int(len(batches)),
        "expiry_completeness": float(dated_lines / lines) if lines else 0.0,
        "status_batches": {s: int(by_status["size"].get(s, 0)) for s in EXPIRY_STATUS_ORDER},
        "status_qty": {s: round(float(by_status["sum"].get(s, 0.0)), 3) for s in EXPIRY_STATUS_ORDER},
        "pull_batches": int(batches["PullFlag"].sum()),
        "at_risk_batches": int(batches["AtRisk"].sum()),
        "projected_waste_qty": round(float(batches["ProjectedWaste"].sum(skipna=True)), 3),
        "usable_qty": round(float(batches.loc[batches["IsUsable"], "Qty"].sum()), 3),
        "usable_without_demand_data_batches": int((batches["IsUsable"] & ~batches["HasDemandData"]).sum()),
        "usable_zero_demand_batches": int((batches["IsUsable"] & batches["DailyDemand"].eq(0)).sum()),
        "negative_qty_batches": int((batches["Qty"] < 0).sum()),
        "unit_cost_available": bool(batches["UnitCost"].notna().any()),
    }


# --------------------------------------------------------------------------
# Refresh: read the warehouse, write the batch tables
# --------------------------------------------------------------------------
def refresh(data_dir: Path = DATA_DIR, config: str | Path | None = CONFIG_PATH, refresh_db: bool = True) -> dict[str, Any]:
    """Rebuild fact_batch_expiry from the latest fact_inventory_snapshot and
    replace its date in fact_batch_expiry_history.

    Returns the summary that the CLI prints; also the payload the Dagster
    ``batch_expiry`` asset turns into run metadata."""
    data_dir = Path(data_dir)

    def read(name: str) -> pd.DataFrame | None:
        path = data_dir / f"{name}.parquet"
        return pd.read_parquet(path) if path.exists() else None

    inventory = read("fact_inventory_snapshot")
    if inventory is None or inventory.empty:
        raise FileNotFoundError(f"{data_dir / 'fact_inventory_snapshot.parquet'} is missing or empty; run refresh_inventory.py first.")
    metadata_path = data_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
    # The date fact_sku_status carries, so the two facts reconcile.
    snapshot_date = metadata.get("snapshot_date") or str(pd.to_datetime(inventory["SnapshotDate"]).max().date())

    cfg = ExpiryConfig.load(config)
    history = read("fact_batch_expiry_history")
    fact_branch_rop = read("fact_branch_rop")
    batches = build_batch_expiry(
        inventory, read("dim_sku"), fact_branch_rop if fact_branch_rop is not None else pd.DataFrame(),
        snapshot_date, cfg, history,
    )
    history = upsert_history(history, batches)
    write_tables(
        {
            "fact_batch_expiry": batches,
            "fact_batch_expiry_history": history,
            "dim_expiry_status": build_dim_expiry_status(cfg),
        },
        data_dir=data_dir, refresh_db=refresh_db,
    )

    expiry = pd.to_datetime(inventory["ExpiryDate"], errors="coerce") if "ExpiryDate" in inventory else pd.Series(dtype="datetime64[ns]")
    summary = {
        "snapshot_date": pd.Timestamp(snapshot_date).date().isoformat(),
        "source_file": str(inventory["SourceFile"].iloc[0]) if "SourceFile" in inventory else "",
        "has_expiry_dates": bool(expiry.notna().any()),
        **summarize(batches),
        "implausible_expiry_lines": int(implausible_expiry(expiry, snapshot_date, cfg).sum()),
        "history_rows": int(len(history)),
        "config_version": cfg.version,
    }
    metadata["expiry"] = summary
    save_json(metadata_path, metadata)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Build fact_batch_expiry from the latest inventory snapshot.")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="Expiry thresholds (JSON)")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    args = parser.parse_args()
    summary = refresh(args.data_dir, config=args.config)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
