"""Pure metric calculations shared by the dashboard and the tests (no Bokeh UI)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from common import STATUS_ORDER
from queries import TOTAL
from warehouse import RISK_STATUSES


class MetricsCalculator:
    @staticmethod
    def calculate_kpis(status: pd.DataFrame) -> dict[str, float]:
        """Reference pandas implementation of DashboardQuery.kpis."""
        if status.empty:
            return {"sku_count": 0.0, "inventory_position": 0.0, "total_rop": 0.0, "rop_gap": 0.0, "critical": 0.0, "excess": 0.0}
        above = (status["ROP"] > 0) & (status["InventoryPosition"] > status["ROP"])
        return {
            "sku_count": float(status["SKU_ID"].nunique()),
            "inventory_position": float(status["InventoryPosition"].sum(skipna=True)),
            "total_rop": float(status["ROP"].sum(skipna=True)),
            "rop_gap": float(status["ROPGap"].sum(skipna=True)),
            "critical": float(status["IsCritical"].fillna(False).astype(bool).sum()),
            "excess": float((status["InventoryPosition"] - status["ROP"])[above].sum()),
        }

    @staticmethod
    def auto_top_measure(statuses: list[str] | tuple[str, ...], critical_only: bool = False) -> str:
        """Which ranking fits the status filter: shortage ('gap') when an at-risk
        status is in view, stock above ROP ('excess') for normal / excess stock,
        plain stock ('stock') when only 'ROP байхгүй' rows are in view, and
        ROP of rows without inventory ('missing') for 'Өгөгдөл алга'."""
        selected = set(statuses) or set(STATUS_ORDER)
        if critical_only or selected & set(RISK_STATUSES):
            return "gap"
        if selected & {"Илүүдэл", "Хэвийн"}:
            return "excess"
        if "ROP байхгүй" in selected:
            return "stock"
        if "Өгөгдөл алга" in selected:
            return "missing"
        return "gap"

    @staticmethod
    def finalize_coverage(sums: pd.DataFrame) -> pd.DataFrame:
        """Coverage sums (DashboardQuery.coverage_summary) -> ratios.

        Only SKU x branch rows with ROP > 0 count; rows with no inventory
        record count as zero stock.
          AvgCoverage   mean of min(Inventory / ROP, 1)
          CoveredShare  share of rows with Inventory >= ROP
          FillRate      sum(min(Inventory, ROP)) / sum(ROP)  (ROP-weighted)
          Gap           sum(ROP) - sum(min(Inventory, ROP))  (units short of ROP)
        """
        out = sums.copy()
        eligible = out["EligibleRows"].where(out["EligibleRows"] > 0)
        out["AvgCoverage"] = out["RatioSum"] / eligible
        out["CoveredShare"] = out["CoveredRows"] / eligible
        out["FillRate"] = out["Filled"] / out["ROP"].where(out["ROP"] > 0)
        out["Gap"] = (out["ROP"] - out["Filled"]).clip(lower=0)
        return out.reset_index(drop=True)

    @staticmethod
    def heatmap_cells(cells: pd.DataFrame) -> pd.DataFrame:
        """Class x Status counts (Class, Status, Count) -> plus Share of the
        class row and Intensity = count relative to the largest cell, for the
        VED / ABC / XYZ x Status heatmaps."""
        out = cells.copy()
        row_total = out.groupby("Class")["Count"].transform("sum")
        out["Share"] = (out["Count"] / row_total.where(row_total > 0)).fillna(0)
        out["Intensity"] = out["Count"] / max(int(out["Count"].max()), 1) if len(out) else out["Count"]
        return out

    @staticmethod
    def class_rollup(breakdown: pd.DataFrame, column: str, order: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
        """ABC x XYZ x Status sums (DashboardQuery.class_breakdown) rolled up
        to one class column, 'ABC' or 'XYZ' -> (cells, classes).

        cells    one row per class x status, ready for the heatmap (heatmap_cells)
        classes  the grand total (Class = TOTAL) first, then the classes in ``order``:
                 SalesShare   share of the 7-month sales of all rows shown
                 CoveredShare rows with Inventory >= ROP / rows with ROP > 0
                 RiskShare    at-risk rows / all rows of the class
        """
        measures = [c for c in breakdown.columns if c not in ("ABC", "XYZ", "Status")]
        cells = breakdown.groupby([column, "Status"], as_index=False)[measures].sum()
        cells = MetricsCalculator.heatmap_cells(cells.rename(columns={column: "Class", "Rows": "Count"}))
        classes = breakdown.groupby(column, as_index=False)[measures].sum().rename(columns={column: "Class"})
        position = {c: i for i, c in enumerate(order)}
        classes = classes.assign(Order=classes["Class"].map(position).fillna(len(order)))
        classes = classes.sort_values("Order", kind="stable").drop(columns="Order")
        if len(classes):
            total = classes[measures].sum().to_frame().T.assign(Class=TOTAL)
            classes = pd.concat([total, classes], ignore_index=True)
        total_sales = float(classes["Sales"].iloc[0]) if len(classes) else 0.0
        classes["SalesShare"] = classes["Sales"] / total_sales if total_sales > 0 else np.nan
        classes["CoveredShare"] = classes["CoveredRows"] / classes["EligibleRows"].where(classes["EligibleRows"] > 0)
        classes["RiskShare"] = classes["RiskRows"] / classes["Rows"]
        return cells, classes.reset_index(drop=True)

    @staticmethod
    def unique_bar_labels(names: list[str], hints: list[str]) -> list[str]:
        """Bokeh's categorical FactorRange requires every axis label to be
        unique. When the same product name repeats (e.g. the same SKU is
        critically low at more than one branch), disambiguate by appending the
        branch name; if that still collides (or there's no branch), fall back
        to a numeric suffix so uniqueness is always guaranteed."""
        counts: dict[str, int] = {}
        for n in names:
            counts[n] = counts.get(n, 0) + 1
        seen: dict[str, int] = {}
        result = []
        for name, hint in zip(names, hints):
            label = f"{name} — {hint}" if counts[name] > 1 and hint else name
            seen[label] = seen.get(label, 0) + 1
            if seen[label] > 1:
                label = f"{label} ({seen[label]})"
            result.append(label)
        return result

    @staticmethod
    def trend_table(trend: pd.DataFrame) -> pd.DataFrame:
        """Long (DateKey, Status) rows -> one row per date with a column per status,
        plus total ROP gap, critical V/E and risk share."""
        if trend.empty:
            return pd.DataFrame(columns=["DateKey", "Date", *STATUS_ORDER, "Total", "Gap", "Critical", "RiskShare", "Scope"])
        wide = trend.pivot_table(index="DateKey", columns="Status", values="Count", aggfunc="sum", fill_value=0)
        wide = wide.reindex(columns=STATUS_ORDER, fill_value=0)
        totals = trend.groupby("DateKey").agg(Gap=("Gap", "sum"), Critical=("Critical", "sum"), Scope=("Scope", "first"))
        out = wide.join(totals).reset_index()
        out["Total"] = out[STATUS_ORDER].sum(axis=1)
        out["RiskShare"] = np.where(out["Total"] > 0, out[list(RISK_STATUSES)].sum(axis=1) / out["Total"].where(out["Total"] > 0), 0.0)
        out["Date"] = pd.to_datetime(out["DateKey"].astype(str), format="%Y%m%d")
        return out

    @staticmethod
    def delta(current: float, previous: float | None) -> str:
        if previous is None or pd.isna(previous):
            return "—"
        diff = current - previous
        sign = "+" if diff > 0 else ""
        return f"{sign}{diff:,.0f}"
