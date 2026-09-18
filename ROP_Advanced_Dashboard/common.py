from __future__ import annotations

import base64
import io
import json
import math
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
from rapidfuzz import fuzz, process

STATUS_ORDER = [
    "Тасарсан",
    "ROP-оос доош",
    "Өгөгдөл алга",
    "ROP байхгүй",
    "Хэвийн",
    "Илүүдэл",
]

VED_ORDER = ["V", "E", "D", "Тодорхойгүй"]
VED_Z = {"V": 2.326, "E": 1.751, "D": 1.175}
VED_WEIGHT = {"V": 3.0, "E": 2.0, "D": 1.0, "Тодорхойгүй": 1.0}
STATUS_WEIGHT = {
    "Тасарсан": 100.0,
    "ROP-оос доош": 70.0,
    "Өгөгдөл алга": 55.0,
    "ROP байхгүй": 45.0,
    "Хэвийн": 10.0,
    "Илүүдэл": 5.0,
}

NAME_ALIASES = {
    "sku_id": ["sku id", "sku_id", "skuid", "барааны код", "материалын код", "код"],
    "product_name": ["нэр төрөл", "бараа материал", "барааны нэр", "product name", "productname", "item name", "itemname", "name"],
    "branch": ["салбар", "агуулах", "location", "branch", "branchname", "store", "warehouse"],
    "manufacturer": ["үйлдвэрлэгч", "manufacturer"],
    "supplier": ["нийлүүлэгч", "поставщик", "supplier", "vendor"],
    "on_hand": ["үлдэгдэл", "эцсийн үлдэгдэл", "on hand", "on_hand", "onhand", "closing stock", "closingstock", "quantity", "qty", "тоо"],
    "on_order": ["замд", "захиалсан", "on order", "on_order", "onorder", "open po", "openpo"],
    "backorder": ["backorder", "back order", "дутагдал", "хүлээгдэж буй"],
    "snapshot_date": ["огноо", "snapshot date", "snapshot_date", "snapshotdate", "date"],
}


def normalize_product_name(value: Any) -> str:
    """Match the normalization logic used in the Excel prototype."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    text = str(value).strip().replace("№", "N")
    for token in [" ", "-", "–", "—", "/", ".", ",", "(", ")", "[", "]", "_", ":"]:
        text = text.replace(token, "")
    return text.upper()


EXCLUDED_BRANCH_KEYS = {
    normalize_product_name(name)
    for name in [
        "Мед_байгууллагын борлуулалт",
        "Мед_Имарт 10-р хороолол",
        "Мед_Имарт Сансар",
        "ЭНБА бат фарм - Жем кастел э/сан",
        "Мед_Премиум э/сан",
        "АФ Саргун-Асрангуй Блю Мон"
    ]
}


def normalize_header(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    text = str(value).strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def clean_category(value: Any) -> str:
    """Return a stable, user-facing category label for source variants/codes."""
    if value is None or pd.isna(value):
        return "Бусад"
    text = str(value).strip()
    if not text or normalize_product_name(text) in {"0X17", "ТОДОРХОЙГҮЙ"}:
        return "Бусад"
    if normalize_product_name(text) == "ЭКС":
        return "Экс"
    return text


def is_excluded_branch(value: Any) -> bool:
    return normalize_product_name(value) in EXCLUDED_BRANCH_KEYS


def branch_group(name: Any) -> str:
    text = "" if pd.isna(name) else str(name).strip()
    if text == "NETWORK":
        return "Сүлжээ"
    if text.startswith("Мед_"):
        return "Мед"
    if text.startswith("АФ "):
        return "Ази Фарма"
    if "ЭНБА" in text:
        return "ЭНБА"
    if "Эрдэнэт" in text or "Орхон" in text:
        return "Орон нутаг"
    return "Бусад"


def _matches_alias(header: str, aliases: Iterable[str]) -> bool:
    header_norm = normalize_header(header)
    parts = [normalize_header(part) for part in str(header).split("|")]
    for alias in aliases:
        alias_norm = normalize_header(alias)
        if header_norm == alias_norm or alias_norm in parts:
            return True
        # Allow compact technical headers such as ``Total OnHand`` without
        # treating long instruction sentences as column headers.
        if len(alias_norm) >= 5 and alias_norm in header_norm and len(header_norm) <= len(alias_norm) + 18:
            return True
    return False


def _find_column(columns: Iterable[Any], key: str) -> Any | None:
    aliases = NAME_ALIASES[key]
    for col in columns:
        if _matches_alias(col, aliases):
            return col
    return None


def _flatten_multirow_headers(raw: pd.DataFrame, start_row: int, depth: int = 3) -> list[str]:
    headers: list[str] = []
    for col_idx in range(raw.shape[1]):
        parts: list[str] = []
        for row_idx in range(start_row, min(start_row + depth, len(raw))):
            val = raw.iat[row_idx, col_idx]
            if pd.notna(val):
                part = str(val).strip()
                if part and part.lower() != "nan" and part not in parts:
                    parts.append(part)
        headers.append(" | ".join(parts))
    return headers


def _detect_header_row(raw: pd.DataFrame, max_rows: int = 40) -> tuple[int, list[str]] | None:
    best: tuple[int, list[str], int] | None = None
    for row_idx in range(min(max_rows, len(raw))):
        headers = _flatten_multirow_headers(raw, row_idx, depth=3)
        score = 0
        for key in ["product_name", "on_hand", "manufacturer", "supplier", "branch", "sku_id"]:
            if _find_column(headers, key) is not None:
                score += 1
        if _find_column(headers, "product_name") is not None and _find_column(headers, "on_hand") is not None:
            score += 4
        if best is None or score >= best[2]:
            best = (row_idx, headers, score)
    if best and best[2] >= 5:
        return best[0], best[1]
    return None


def _read_excel_raw(source: str | Path | bytes, filename: str | None = None) -> tuple[pd.DataFrame, str]:
    if isinstance(source, (str, Path)):
        path = Path(source)
        suffix = path.suffix.lower()
        engine = "pyxlsb" if suffix == ".xlsb" else "openpyxl"
        xls = pd.ExcelFile(path, engine=engine)
        candidates = xls.sheet_names
        last_error: Exception | None = None
        for sheet in candidates:
            try:
                raw = pd.read_excel(path, sheet_name=sheet, header=None, engine=engine)
                if _detect_header_row(raw) is not None:
                    return raw, sheet
            except Exception as exc:  # pragma: no cover - defensive
                last_error = exc
        if last_error:
            raise last_error
        raise ValueError("Тохирох үлдэгдлийн хүснэгт олдсонгүй.")

    suffix = Path(filename or "upload.xlsx").suffix.lower()
    if suffix == ".xlsb":
        with tempfile.NamedTemporaryFile(suffix=".xlsb", delete=False) as tmp:
            tmp.write(source)
            tmp_path = Path(tmp.name)
        try:
            return _read_excel_raw(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)
    bio = io.BytesIO(source)
    xls = pd.ExcelFile(bio, engine="openpyxl")
    for sheet in xls.sheet_names:
        bio.seek(0)
        raw = pd.read_excel(bio, sheet_name=sheet, header=None, engine="openpyxl")
        if _detect_header_row(raw) is not None:
            return raw, sheet
    raise ValueError("Тохирох үлдэгдлийн хүснэгт олдсонгүй.")


def parse_inventory(source: str | Path | bytes, filename: str | None = None) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Parse a simple CSV/XLSX/XLSB inventory export into a standard schema."""
    source_name = filename or (Path(source).name if isinstance(source, (str, Path)) else "upload")
    suffix = Path(source_name).suffix.lower()

    if suffix == ".csv":
        if isinstance(source, bytes):
            bio = io.BytesIO(source)
            try:
                frame = pd.read_csv(bio, encoding="utf-8-sig")
            except UnicodeDecodeError:
                bio.seek(0)
                frame = pd.read_csv(bio, encoding="cp1251")
        else:
            try:
                frame = pd.read_csv(source, encoding="utf-8-sig")
            except UnicodeDecodeError:
                frame = pd.read_csv(source, encoding="cp1251")
        header_row = 0
        sheet = "CSV"
    else:
        raw, sheet = _read_excel_raw(source, filename=source_name)
        detected = _detect_header_row(raw)
        if detected is None:
            raise ValueError("Үлдэгдлийн багануудыг таньж чадсангүй.")
        header_row, headers = detected
        direct_headers = [str(v).strip() if pd.notna(v) else "" for v in raw.iloc[header_row].tolist()]
        direct_simple = (
            (_find_column(direct_headers, "product_name") is not None or _find_column(direct_headers, "sku_id") is not None)
            and _find_column(direct_headers, "on_hand") is not None
        )
        data_start = header_row + 1 if direct_simple else header_row + 3
        frame = raw.iloc[data_start:].copy()
        frame.columns = direct_headers if direct_simple else headers
        # Remove header-like rows that may remain in the data.
        frame = frame.loc[~frame.apply(lambda r: any(str(v).strip().lower() in {"тоо", "эцсийн үлдэгдэл"} for v in r if pd.notna(v)), axis=1)]

    cols = list(frame.columns)
    col_map = {
        key: _find_column(cols, key)
        for key in ["sku_id", "product_name", "branch", "manufacturer", "supplier", "on_hand", "on_order", "backorder", "snapshot_date"]
    }

    if col_map["product_name"] is None and col_map["sku_id"] is None:
        raise ValueError("SKU ID эсвэл Нэр төрөл багана олдсонгүй.")
    if col_map["on_hand"] is None:
        raise ValueError("Үлдэгдэл / Эцсийн үлдэгдэл багана олдсонгүй.")

    out = pd.DataFrame(index=frame.index)
    out["SKU_ID"] = pd.to_numeric(frame[col_map["sku_id"]], errors="coerce") if col_map["sku_id"] is not None else np.nan
    out["InventoryName"] = frame[col_map["product_name"]].astype(str).str.strip() if col_map["product_name"] is not None else ""
    out["BranchName"] = frame[col_map["branch"]].astype(str).str.strip() if col_map["branch"] is not None else "NETWORK"
    out["Manufacturer"] = frame[col_map["manufacturer"]].astype(str).str.strip() if col_map["manufacturer"] is not None else ""
    out["Supplier"] = frame[col_map["supplier"]].astype(str).str.strip() if col_map["supplier"] is not None else ""
    out["OnHand"] = pd.to_numeric(frame[col_map["on_hand"]], errors="coerce")
    out["OnOrder"] = pd.to_numeric(frame[col_map["on_order"]], errors="coerce").fillna(0) if col_map["on_order"] is not None else 0.0
    out["Backorder"] = pd.to_numeric(frame[col_map["backorder"]], errors="coerce").fillna(0) if col_map["backorder"] is not None else 0.0
    if col_map["snapshot_date"] is not None:
        out["SnapshotDate"] = pd.to_datetime(frame[col_map["snapshot_date"]], errors="coerce")
    else:
        out["SnapshotDate"] = pd.NaT

    out = out[out["OnHand"].notna()].copy()
    out = out[(out["InventoryName"].astype(str).str.lower() != "nan") | out["SKU_ID"].notna()].copy()
    out["BranchName"] = out["BranchName"].replace({"": "NETWORK", "nan": "NETWORK"}).fillna("NETWORK")
    out["NormalizedKey"] = out["InventoryName"].map(normalize_product_name)
    out["InventoryPosition"] = out["OnHand"] + out["OnOrder"] - out["Backorder"]
    out.reset_index(drop=True, inplace=True)
    out.insert(0, "InventoryLineID", np.arange(1, len(out) + 1))

    meta = {
        "source_file": source_name,
        "sheet": sheet,
        "header_row_zero_based": int(header_row),
        "column_map": {k: (str(v) if v is not None else None) for k, v in col_map.items()},
        "scope": "BRANCH" if (out["BranchName"] != "NETWORK").any() else "NETWORK",
        "rows": int(len(out)),
    }
    return out, meta


def map_inventory(
    inventory: pd.DataFrame,
    alias_mapping: pd.DataFrame,
    dim_sku: pd.DataFrame,
    dim_branch: pd.DataFrame | None = None,
    fuzzy_limit: int = 1000,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    alias = alias_mapping.copy()
    alias = alias.dropna(subset=["NormalizedKey", "SKU_ID"])
    alias["NormalizedKey"] = alias["NormalizedKey"].astype(str)
    alias["SKU_ID"] = pd.to_numeric(alias["SKU_ID"], errors="coerce").astype("Int64")
    alias_map = alias.drop_duplicates("NormalizedKey").set_index("NormalizedKey")["SKU_ID"].to_dict()

    sku_ids = set(pd.to_numeric(dim_sku["SKU_ID"], errors="coerce").dropna().astype(int))
    mapped = inventory.copy()
    direct_valid = pd.to_numeric(mapped["SKU_ID"], errors="coerce").isin(sku_ids)
    mapped.loc[~direct_valid, "SKU_ID"] = mapped.loc[~direct_valid, "NormalizedKey"].map(alias_map)
    mapped["SKU_ID"] = pd.to_numeric(mapped["SKU_ID"], errors="coerce").astype("Int64")
    mapped["MatchStatus"] = np.where(direct_valid, "SKU ID", np.where(mapped["SKU_ID"].notna(), "Alias exact", "Unmatched"))

    branch_id_map: dict[str, str] = {"NETWORK": "NETWORK"}
    if dim_branch is not None and not dim_branch.empty:
        branch_id_map.update(dict(zip(dim_branch["BranchName"], dim_branch["Branch_ID"])))
    mapped["Branch_ID"] = mapped["BranchName"].map(branch_id_map)
    mapped.loc[mapped["Branch_ID"].isna(), "Branch_ID"] = mapped.loc[mapped["Branch_ID"].isna(), "BranchName"].map(lambda x: f"UNMAPPED::{normalize_product_name(x)[:80]}")

    unmatched = mapped[mapped["SKU_ID"].isna()].copy()
    review_cols = ["InventoryName", "Manufacturer", "Supplier", "OnHand", "NormalizedKey", "BranchName"]
    review = unmatched[review_cols].copy()
    review["Suggested_SKU_ID"] = pd.NA
    review["Suggested_SKU_Name"] = ""
    review["Similarity"] = np.nan
    review["Decision"] = "Гараар шалгах"

    if len(review) and fuzzy_limit > 0:
        master_choices = dim_sku.set_index("MasterKey")["SKU_Name"].to_dict()
        keys = list(master_choices.keys())
        suggestions = []
        for idx, key in review["NormalizedKey"].head(fuzzy_limit).items():
            match = process.extractOne(key, keys, scorer=fuzz.ratio) if key else None
            if match:
                matched_key, score, _ = match
                sku_row = dim_sku.loc[dim_sku["MasterKey"] == matched_key].iloc[0]
                suggestions.append((idx, int(sku_row["SKU_ID"]), str(sku_row["SKU_Name"]), float(score)))
        for idx, sku_id, sku_name, score in suggestions:
            review.loc[idx, ["Suggested_SKU_ID", "Suggested_SKU_Name", "Similarity"]] = [sku_id, sku_name, score]

    qty_total = float(mapped["OnHand"].sum()) if len(mapped) else 0.0
    qty_matched = float(mapped.loc[mapped["SKU_ID"].notna(), "OnHand"].sum()) if len(mapped) else 0.0
    stats = {
        "inventory_rows": int(len(mapped)),
        "matched_rows": int(mapped["SKU_ID"].notna().sum()),
        "unmatched_rows": int(mapped["SKU_ID"].isna().sum()),
        "mapping_rate_rows": float(mapped["SKU_ID"].notna().mean()) if len(mapped) else 0.0,
        "total_on_hand": qty_total,
        "matched_on_hand": qty_matched,
        "mapping_rate_qty": qty_matched / qty_total if qty_total else 0.0,
        "negative_inventory_rows": int((mapped["OnHand"] < 0).sum()),
        "scope": "BRANCH" if (mapped["BranchName"] != "NETWORK").any() else "NETWORK",
    }
    return mapped, review.reset_index(drop=True), stats


def classify_status(on_hand: float | None, rop: float | None, excess_threshold: float = 2.0, has_data: bool = True) -> str:
    if not has_data or on_hand is None or pd.isna(on_hand):
        return "Өгөгдөл алга"
    rop_value = 0.0 if rop is None or pd.isna(rop) else float(rop)
    on_hand_value = float(on_hand)
    if rop_value <= 0:
        return "ROP байхгүй"
    if on_hand_value <= 0:
        return "Тасарсан"
    if on_hand_value < rop_value:
        return "ROP-оос доош"
    if on_hand_value >= rop_value * excess_threshold:
        return "Илүүдэл"
    return "Хэвийн"


def build_status_snapshot(
    dim_sku: pd.DataFrame,
    mapped_inventory: pd.DataFrame,
    fact_branch_rop: pd.DataFrame,
    snapshot_date: str | pd.Timestamp,
    excess_threshold: float = 2.0,
) -> pd.DataFrame:
    mapped_inventory = mapped_inventory.loc[
        ~mapped_inventory["BranchName"].map(is_excluded_branch)
    ].copy()
    fact_branch_rop = fact_branch_rop.loc[
        ~fact_branch_rop["BranchName"].map(is_excluded_branch)
    ].copy()
    snapshot_date = pd.Timestamp(snapshot_date).date().isoformat()
    scope = "BRANCH" if (mapped_inventory["BranchName"] != "NETWORK").any() else "NETWORK"

    matched = mapped_inventory[mapped_inventory["SKU_ID"].notna()].copy()
    matched["SKU_ID"] = matched["SKU_ID"].astype(int)
    grouped = matched.groupby(["Branch_ID", "BranchName", "SKU_ID"], as_index=False).agg(
        OnHand=("OnHand", "sum"),
        OnOrder=("OnOrder", "sum"),
        Backorder=("Backorder", "sum"),
        InventoryPosition=("InventoryPosition", "sum"),
        InventoryLines=("InventoryLineID", "count"),
        Manufacturer=("Manufacturer", lambda x: "; ".join(sorted({str(v) for v in x if str(v) not in {"", "nan"}}))[:500]),
        Supplier=("Supplier", lambda x: "; ".join(sorted({str(v) for v in x if str(v) not in {"", "nan"}}))[:500]),
    )

    if scope == "NETWORK":
        base = dim_sku[[
            "SKU_ID", "SKU_Name", "Category", "VED", "ROP_Used", "ROP_Source", "ROP_Confidence",
            "LeadTime_Months", "LeadTime_Status", "Sales_7M", "AvgMonthlyDemand", "MasterKey",
        ]].copy()
        base["Branch_ID"] = "NETWORK"
        base["BranchName"] = "NETWORK"
        base.rename(columns={"ROP_Used": "ROP"}, inplace=True)
        status = base.merge(grouped, on=["Branch_ID", "BranchName", "SKU_ID"], how="left")
    else:
        rop = fact_branch_rop.copy()
        status = rop.merge(grouped, on=["Branch_ID", "BranchName", "SKU_ID"], how="outer")
        status = status.merge(
            dim_sku[["SKU_ID", "SKU_Name", "ROP_Source", "ROP_Confidence", "LeadTime_Status", "MasterKey", "VED", "Category"]],
            on="SKU_ID", how="left", suffixes=("", "_Master"),
        )
        if "VED_Master" in status:
            status["VED"] = status["VED"].fillna(status["VED_Master"])
        if "Category_Master" in status:
            status["Category"] = status["Category"].fillna(status["Category_Master"])
        status["ROP"] = pd.to_numeric(status.get("ROP"), errors="coerce").fillna(0)
        status["LeadTime_Months"] = status.get("LeadTimeMean")
        status["Sales_7M"] = status.get("Sales7M")
        status["AvgMonthlyDemand"] = status.get("AvgMonthlyDemand_Branch")

    status["DataMatch"] = np.where(status["OnHand"].notna(), "Matched", "Missing")
    status["ROPGap"] = np.where(status["OnHand"].notna(), np.maximum(status["ROP"].fillna(0) - status["InventoryPosition"].fillna(0), 0), np.nan)
    status["CoverageRatio"] = np.where(
        status["ROP"].fillna(0) > 0,
        (status["InventoryPosition"] / status["ROP"]).clip(lower=0, upper=1),
        np.nan,
    )
    status["Category"] = status["Category"].map(clean_category)
    status["Status"] = [
        classify_status(on, rop, excess_threshold=excess_threshold, has_data=has)
        for on, rop, has in zip(status["InventoryPosition"], status["ROP"], status["OnHand"].notna())
    ]
    status["WeightedGap"] = status["ROPGap"].fillna(0) * status["VED"].map(VED_WEIGHT).fillna(1.0)
    status["PriorityScore"] = (
        status["Status"].map(STATUS_WEIGHT).fillna(0)
        + status["VED"].map(VED_WEIGHT).fillna(1.0) * 10
        + np.minimum(status["WeightedGap"].fillna(0), 10000) / 100
        + np.where(status["ROP_Confidence"] == "Бага", 5, 0)
    )
    status["SnapshotDate"] = snapshot_date
    status["Scope"] = scope
    status["BranchGroup"] = status["BranchName"].map(branch_group)
    status["IsCritical"] = status["Status"].isin(["Тасарсан", "ROP-оос доош"]) & status["VED"].isin(["V", "E"])
    status["HasROP"] = status["ROP"].fillna(0) > 0
    status["HasInventory"] = status["OnHand"].notna()
    return status


def load_project_data(data_dir: str | Path) -> dict[str, pd.DataFrame]:
    data_dir = Path(data_dir)
    frames = {}
    for name in ["dim_sku", "dim_branch", "fact_branch_rop", "fact_inventory_snapshot", "fact_sku_status", "alias_mapping", "mapping_review"]:
        path_gz = data_dir / f"{name}.csv.gz"
        path_csv = data_dir / f"{name}.csv"
        path = path_gz if path_gz.exists() else path_csv
        if path.exists():
            frames[name] = pd.read_csv(path, low_memory=False)
    return frames


def dataframe_to_csv_data_uri(df: pd.DataFrame) -> str:
    csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
    encoded = base64.b64encode(csv_bytes).decode("ascii")
    return f"data:text/csv;base64,{encoded}"


def save_json(path: str | Path, payload: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
