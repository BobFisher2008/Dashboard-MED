"""Pure metric calculations shared by the dashboard and the tests (no Bokeh UI)."""
from __future__ import annotations

import numpy as np
import pandas as pd
from bokeh.util.hex import hexbin

from common import STATUS_ORDER
from warehouse import RISK_STATUSES


class MetricsCalculator:
    @staticmethod
    def calculate_kpis(status: pd.DataFrame) -> dict[str, float]:
        """Reference pandas implementation of DashboardQuery.kpis."""
        if status.empty:
            return {"sku_count": 0.0, "inventory_position": 0.0, "total_rop": 0.0, "rop_gap": 0.0, "critical": 0.0}
        return {
            "sku_count": float(status["SKU_ID"].nunique()),
            "inventory_position": float(status["InventoryPosition"].sum(skipna=True)),
            "total_rop": float(status["ROP"].sum(skipna=True)),
            "rop_gap": float(status["ROPGap"].sum(skipna=True)),
            "critical": float(status["IsCritical"].fillna(False).astype(bool).sum()),
        }

    @staticmethod
    def finalize_coverage(grouped: pd.DataFrame) -> pd.DataFrame:
        """Branch x category sums -> average coverage ratio and gap."""
        out = grouped.copy()
        out["Coverage"] = np.where(out["EligibleSKU"] > 0, out["CoverageRatio"] / out["EligibleSKU"].where(out["EligibleSKU"] > 0), np.nan)
        out["Gap"] = (out["ROP"] - out["Inventory"]).clip(lower=0)
        return out.sort_values(["BranchName", "Category"]).reset_index(drop=True)

    @staticmethod
    def hexbin_log(rop: pd.Series, inventory: pd.Series, size: float) -> tuple[list, list, list]:
        """Hex-bin SKUs on log10(x + 1) axes; returns (q, r, counts)."""
        if len(rop) == 0:
            return [], [], []
        x = np.log10(pd.Series(rop).fillna(0).clip(lower=0).to_numpy() + 1)
        y = np.log10(pd.Series(inventory).fillna(0).clip(lower=0).to_numpy() + 1)
        bins = hexbin(x, y, size=size)
        # Bokeh >=3.9 returns a dataclass (bins.q); older versions a DataFrame.
        q, r, counts = (bins.q, bins.r, bins.counts) if hasattr(bins, "q") else (bins["q"], bins["r"], bins["counts"])
        return list(q), list(r), list(counts)

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
