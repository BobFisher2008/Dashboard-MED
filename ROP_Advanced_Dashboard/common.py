from __future__ import annotations

import base64
import io
import json
import math
import re
import tempfile
from datetime import date
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

RISK_STATUSES = ("Тасарсан", "ROP-оос доош")

VED_ORDER = ["V", "E", "D", "Тодорхойгүй"]
VED_CLASSES = ("V", "E", "D")  # most critical first
# dim_sku.VED_Source: where a SKU's VED comes from. SKUs listed in the VED file
# carry that VED into every fact; the rest keep the ROP workbook's value.
VED_FILE_SOURCE = "VED ангилал"
VED_MASTER_SOURCE = "ROP мастер"
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
    "on_hand": ["үлдэгдэл", "эцсийн үлдэгдэл", "on hand", "on_hand", "onhand", "closing stock", "closingstock", "quantity", "qty", "тоо","Total","total"],
    "on_order": ["замд", "захиалсан", "on order", "on_order", "onorder", "open po", "openpo"],
    "backorder": ["backorder", "back order", "дутагдал", "хүлээгдэж буй"],
    "snapshot_date": ["огноо", "snapshot date", "snapshot_date", "snapshotdate", "date"],
    # Batch expiry ("Сери.Хүртэл хүчинтэй" = series valid until), most specific
    # first: _find_expiry_column takes the first alias that some column has.
    # The generic words (EXPIRY_GENERIC_ALIASES) only match a whole header.
    "expiry": ["сери.хүртэл хүчинтэй", "хүртэл хүчинтэй", "expiry date", "expiration date", "exp date", "expiry",
               "хүчинтэй хугацаа", "дуусах хугацаа", "дуусах огноо", "хугацаа", "огноо", "date"],
}
EXPIRY_GENERIC_ALIASES = {"хугацаа", "огноо", "date"}


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
    if normalize_product_name(text) in {"ЭКС", "ЭКСКЛЮЗИВ"}:
        return "Эксклюзив"
    return text


# Cyrillic letters typed for V / E / D on a Mongolian keyboard.
_VED_LOOKALIKES = str.maketrans({"В": "V", "Е": "E", "Д": "D"})


def clean_ved(value: Any) -> str | None:
    """'V', 'E' or 'D'; None when the value is not a VED class."""
    if value is None or pd.isna(value):
        return None
    text = str(value).strip().upper().translate(_VED_LOOKALIKES)
    return text if text in VED_CLASSES else None


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


def _find_expiry_column(columns: Iterable[Any]) -> Any | None:
    """The expiry column: the first alias, in NAME_ALIASES order, that a
    header equals; then the specific aliases inside a compact header. So an
    export with both «Сери.Хүртэл хүчинтэй» and «Огноо» takes the former, and
    «Үлдэгдлийн огноо» is never read as an expiry date."""
    columns = list(columns)
    aliases = NAME_ALIASES["expiry"]
    for alias in aliases:
        alias_norm = normalize_header(alias)
        for col in columns:
            if normalize_header(col) == alias_norm or alias_norm in [normalize_header(p) for p in str(col).split("|")]:
                return col
    for alias in aliases:
        if alias in EXPIRY_GENERIC_ALIASES:
            continue
        for col in columns:
            if _matches_alias(col, [alias]):
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


_EXCEL_EPOCH = pd.Timestamp("1899-12-30")
_YMD = re.compile(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})")
_DMY = re.compile(r"(\d{1,2})([-./])(\d{1,2})\2(\d{4}|\d{2})")
_MONTH_YEAR = re.compile(r"(\d{1,2})[-./](\d{4})")
_YEAR_MONTH = re.compile(r"(\d{4})[-./](\d{1,2})")


def _safe_date(year: int, month: int, day: int | None = None) -> pd.Timestamp | None:
    """The date, or the last day of the month when ``day`` is None."""
    try:
        if day is None:
            return pd.Timestamp(year, month, 1) + pd.offsets.MonthEnd(0)
        return pd.Timestamp(year, month, day)
    except ValueError:
        return None


def _parse_expiry(value: Any, month_first: bool) -> pd.Timestamp | None:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    if isinstance(value, (date, np.datetime64)):
        return pd.Timestamp(value).normalize()
    if isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(value, bool):
        # Excel serial day (xlsb cells arrive as plain numbers); 20000..80000 = 1954..2119.
        return pd.to_datetime(int(value), unit="D", origin=_EXCEL_EPOCH) if 20000 <= value <= 80000 else None
    text = str(value).strip().split(" ")[0].split("T")[0]
    if text.isdigit() and len(text) == 5:
        return _parse_expiry(int(text), month_first)
    if m := _YMD.fullmatch(text):
        return _safe_date(int(m[1]), int(m[2]), int(m[3]))
    if m := _DMY.fullmatch(text):
        first, sep, second, year = int(m[1]), m[2], int(m[3]), int(m[4])
        year += 2000 if year < 100 else 0
        month, day = (first, second) if sep == "/" and month_first else (second, first)
        return _safe_date(year, month, day)
    if m := _MONTH_YEAR.fullmatch(text):
        return _safe_date(int(m[2]), int(m[1]))
    if m := _YEAR_MONTH.fullmatch(text):
        return _safe_date(int(m[1]), int(m[2]))
    return None


def parse_expiry_dates(values: pd.Series) -> pd.Series:
    """Expiry dates of an export column as datetime64 (NaT = blank or unreadable).

    Accepts dates, Excel serial numbers and text: m/d/yyyy (the ERP export),
    d/m/yyyy when some value can only be read that way, d.m.yyyy, yyyy-mm-dd,
    and month-only mm/yyyy, which means the last day of that month (the
    "EXP 11/2026" convention)."""
    day_first = any(
        (m := _DMY.fullmatch(v.strip().split(" ")[0])) and m[2] == "/" and int(m[1]) > 12
        for v in pd.unique(values.dropna()) if isinstance(v, str)
    )
    parsed = _map_unique(values, lambda v: _parse_expiry(v, month_first=not day_first))
    return pd.to_datetime(parsed, errors="coerce")


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
    col_map["expiry"] = _find_expiry_column(cols)
    if col_map["expiry"] is not None and col_map["snapshot_date"] == col_map["expiry"]:
        col_map["snapshot_date"] = None  # «Огноо» / «Дуусах огноо» is the expiry date, not the snapshot date

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
    if col_map["expiry"] is not None:
        raw_expiry = frame[col_map["expiry"]]
        out["ExpiryDate"] = parse_expiry_dates(raw_expiry)
        raw_text = raw_expiry.astype(str).str.strip()
        unreadable = out["ExpiryDate"].isna() & raw_expiry.notna() & (raw_text != "")
        out["ExpiryRaw"] = raw_text.where(unreadable, "")  # kept only where it could not be read
    else:
        out["ExpiryDate"] = pd.NaT
        out["ExpiryRaw"] = ""

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


def classify_status_vectorized(
    on_hand: pd.Series, rop: pd.Series, has_data: pd.Series, excess_threshold: float = 2.0
) -> np.ndarray:
    """Array version of ``classify_status`` (same rules, same order)."""
    on_hand = pd.to_numeric(on_hand, errors="coerce")
    rop_value = pd.to_numeric(rop, errors="coerce").fillna(0.0)
    missing = ~has_data.astype(bool) | on_hand.isna()
    conditions = [
        missing,
        rop_value <= 0,
        on_hand <= 0,
        on_hand < rop_value,
        on_hand >= rop_value * excess_threshold,
    ]
    choices = ["Өгөгдөл алга", "ROP байхгүй", "Тасарсан", "ROP-оос доош", "Илүүдэл"]
    return np.select(conditions, choices, default="Хэвийн")


def _map_unique(values: pd.Series, func) -> pd.Series:
    """``values.map(func)`` evaluated once per distinct value (NaN included)."""
    codes, uniques = pd.factorize(values, use_na_sentinel=False)
    mapped = np.array([func(u) for u in uniques], dtype=object)
    return pd.Series(mapped[codes], index=values.index)


def excluded_mask(branches: pd.Series) -> pd.Series:
    return _map_unique(branches, is_excluded_branch).astype(bool)


def ved_measures(ved: pd.Series, rop_gap: pd.Series, status: pd.Series, low_confidence: pd.Series) -> pd.DataFrame:
    """The VED-dependent measures of status rows: WeightedGap (ROP gap x VED
    weight), PriorityScore and IsCritical (an at-risk V or E row)."""
    weight = ved.map(VED_WEIGHT).astype("float64").fillna(1.0)
    weighted = rop_gap.fillna(0) * weight
    return pd.DataFrame({
        "WeightedGap": weighted,
        "PriorityScore": (
            status.map(STATUS_WEIGHT).astype("float64").fillna(0)
            + weight * 10
            + np.minimum(weighted, 10000) / 100
            + np.where(low_confidence, 5, 0)
        ),
        "IsCritical": status.isin(RISK_STATUSES) & ved.isin(["V", "E"]),
    }, index=ved.index)


def _join_unique(frame: pd.DataFrame, keys: list[str], col: str, limit: int = 500) -> pd.DataFrame:
    """'; '-joined sorted distinct non-empty values of ``col`` per key."""
    vals = frame[keys + [col]].copy()
    vals[col] = vals[col].astype(str).str.strip()
    vals = vals[~vals[col].isin(["", "nan", "None", "<NA>"])].drop_duplicates().sort_values(keys + [col])
    if vals.empty:
        return pd.DataFrame(columns=keys + [col])
    joined = vals.groupby(keys, sort=False)[col].agg("; ".join).str[:limit]
    return joined.reset_index()


def build_status_snapshot(
    dim_sku: pd.DataFrame,
    mapped_inventory: pd.DataFrame,
    fact_branch_rop: pd.DataFrame,
    snapshot_date: str | pd.Timestamp,
    excess_threshold: float = 2.0,
) -> pd.DataFrame:
    mapped_inventory = mapped_inventory.loc[~excluded_mask(mapped_inventory["BranchName"])].copy()
    fact_branch_rop = fact_branch_rop.loc[~excluded_mask(fact_branch_rop["BranchName"])].copy()
    snapshot_date = pd.Timestamp(snapshot_date).date().isoformat()
    scope = "BRANCH" if (mapped_inventory["BranchName"] != "NETWORK").any() else "NETWORK"

    matched = mapped_inventory[mapped_inventory["SKU_ID"].notna()].copy()
    matched["SKU_ID"] = matched["SKU_ID"].astype(int)
    keys = ["Branch_ID", "BranchName", "SKU_ID"]
    grouped = matched.groupby(keys, as_index=False).agg(
        OnHand=("OnHand", "sum"),
        OnOrder=("OnOrder", "sum"),
        Backorder=("Backorder", "sum"),
        InventoryPosition=("InventoryPosition", "sum"),
        InventoryLines=("InventoryLineID", "count"),
    )
    for col in ["Manufacturer", "Supplier"]:
        grouped = grouped.merge(_join_unique(matched, keys, col), on=keys, how="left")
        grouped[col] = grouped[col].fillna("")

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
    status["Category"] = _map_unique(status["Category"], clean_category)
    status["Status"] = classify_status_vectorized(
        status["InventoryPosition"], status["ROP"], status["OnHand"].notna(), excess_threshold
    )
    low_confidence = status["ROP_Confidence"].eq("Бага").fillna(False).astype(bool)
    status[["WeightedGap", "PriorityScore", "IsCritical"]] = ved_measures(
        status["VED"], status["ROPGap"], status["Status"], low_confidence
    )
    status["SnapshotDate"] = snapshot_date
    status["Scope"] = scope
    status["BranchGroup"] = _map_unique(status["BranchName"], branch_group)
    status["HasROP"] = status["ROP"].fillna(0) > 0
    status["HasInventory"] = status["OnHand"].notna()
    return status


def apply_missing_as_zero(status: pd.DataFrame) -> pd.DataFrame:
    """Treat SKU x branch rows without inventory data as zero stock."""
    status = status.copy()
    missing = ~status["HasInventory"].fillna(False).astype(bool)
    status.loc[missing, ["OnHand", "OnOrder", "Backorder", "InventoryPosition"]] = 0.0
    status.loc[missing, "ROPGap"] = status.loc[missing, "ROP"].clip(lower=0)
    status.loc[missing, "CoverageRatio"] = np.where(status.loc[missing, "ROP"] > 0, 0.0, np.nan)
    status.loc[missing & (status["ROP"] > 0), "Status"] = "Тасарсан"
    status.loc[missing & (status["ROP"] <= 0), "Status"] = "ROP байхгүй"
    status.loc[missing, "DataMatch"] = "Missing treated as zero"
    status.loc[missing, "HasInventory"] = True
    status["IsCritical"] = status["Status"].isin(RISK_STATUSES) & status["VED"].isin(["V", "E"])
    return status


def _read_source_csv(path: str | Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(path, encoding="cp1251")


def _match_source_names(
    src: pd.DataFrame,
    dim_sku: pd.DataFrame,
    alias_mapping: pd.DataFrame | None,
) -> pd.DataFrame:
    """Add NormalizedKey, SKU_ID and MatchType to rows with a SourceName.

    Exact matches only: the normalized key against the SKU master key, then
    against the alias mapping. No fuzzy matching - in these lists the near
    misses are other pack sizes of the same product. A few SKUs share a key
    (e.g. "Бумба N6" and "Бумба №6"), so one name can match several SKUs;
    MatchType "Exact name" marks the SKU whose own name it is."""
    src = src.copy()
    src["NormalizedKey"] = src["SourceName"].map(normalize_product_name)
    master = dim_sku[["MasterKey", "SKU_ID", "SKU_Name"]].dropna(subset=["MasterKey", "SKU_ID"]).astype({"MasterKey": str})
    src = src.merge(master.rename(columns={"MasterKey": "NormalizedKey"}), on="NormalizedKey", how="left")
    exact = src["SKU_Name"].astype(str).str.strip().eq(src["SourceName"]) & src["SKU_ID"].notna()
    src["MatchType"] = np.select([exact, src["SKU_ID"].notna()], ["Exact name", "Master key"], "Unmatched")
    src = src.drop(columns="SKU_Name")
    if alias_mapping is not None and len(alias_mapping):
        alias = alias_mapping.dropna(subset=["NormalizedKey", "SKU_ID"]).drop_duplicates("NormalizedKey")
        alias_map = alias.set_index(alias["NormalizedKey"].astype(str))["SKU_ID"]
        via_alias = src["SKU_ID"].isna() & src["NormalizedKey"].isin(alias_map.index)
        src.loc[via_alias, "SKU_ID"] = src.loc[via_alias, "NormalizedKey"].map(alias_map)
        src.loc[via_alias, "MatchType"] = "Alias"
    src["SKU_ID"] = pd.to_numeric(src["SKU_ID"], errors="coerce").astype("Int64")
    return src


def load_category_map(
    path: str | Path,
    dim_sku: pd.DataFrame,
    alias_mapping: pd.DataFrame | None = None,
) -> tuple[pd.Series, pd.DataFrame, dict[str, Any]]:
    """Map the category source file (product name, category) onto SKU_IDs.

    Names are matched on the normalized key: first against the SKU master
    key, then against the alias mapping. When one SKU receives more than one
    category, the most frequent one wins and the conflict is reported.
    Returns (category by SKU_ID, review rows, stats).
    """
    raw = _read_source_csv(path)
    src = pd.DataFrame({
        "SourceName": raw.iloc[:, 0].astype(str).str.strip(),
        "SourceCategory": raw.iloc[:, 1].map(clean_category),
    })
    source_rows = len(src)
    src = _match_source_names(src, dim_sku, alias_mapping)

    matched = src[src["SKU_ID"].notna()]
    category = matched.groupby("SKU_ID")["SourceCategory"].agg(lambda x: x.value_counts().index[0])
    conflicts = matched.groupby("SKU_ID")["SourceCategory"].nunique()
    conflict_ids = set(conflicts[conflicts > 1].index)

    review = src[src["SKU_ID"].isna()].assign(Issue="Эх файлын нэр SKU-тэй таарсангүй")
    conflict_rows = matched[matched["SKU_ID"].isin(conflict_ids)].assign(Issue="Олон категори — давамгайг сонгов")
    uncovered = dim_sku.loc[~dim_sku["SKU_ID"].isin(category.index), ["SKU_ID", "SKU_Name", "Category"]]
    uncovered = uncovered.rename(columns={"SKU_Name": "SourceName", "Category": "SourceCategory"})
    uncovered = uncovered.assign(Issue="SKU категори файлд алга — хуучин категори хэвээр")
    review = pd.concat([review, conflict_rows, uncovered], ignore_index=True)[
        ["Issue", "SourceName", "SourceCategory", "SKU_ID", "NormalizedKey", "MatchType"]
    ]
    stats = {
        "source_rows": int(source_rows),
        "matched_rows": int(src.loc[src["SKU_ID"].notna(), "SourceName"].nunique()),
        "unmatched_rows": int(src["SKU_ID"].isna().sum()),
        "sku_covered": int(len(category)),
        "sku_uncovered": int(len(uncovered)),
        "sku_conflicts": int(len(conflict_ids)),
    }
    return category, review, stats


def apply_category_map(dim_sku: pd.DataFrame, category_by_sku: pd.Series) -> pd.DataFrame:
    dim_sku = dim_sku.copy()
    mapped = pd.to_numeric(dim_sku["SKU_ID"], errors="coerce").map(category_by_sku)
    dim_sku["Category"] = mapped.fillna(dim_sku["Category"]).map(clean_category)
    return dim_sku


def load_ved_map(
    path: str | Path,
    dim_sku: pd.DataFrame,
    alias_mapping: pd.DataFrame | None = None,
) -> tuple[pd.Series, pd.DataFrame, dict[str, Any]]:
    """Map the VED classification file (product name in the first column, a
    column headed VED) onto SKU_IDs.

    Names are matched like ``load_category_map``. When one SKU receives more
    than one VED, the row with the SKU's own name wins, then a master-key
    match over an alias, then the most critical (V > E > D); the conflict is
    reported. Rows whose VED is not V / E / D are reported and not applied.
    Returns (VED by SKU_ID, review rows, stats).
    """
    raw = _read_source_csv(path)
    ved_col = next((c for c in raw.columns if normalize_header(c) == "ved"), None)
    if ved_col is None:
        raise ValueError("VED багана олдсонгүй.")
    src = pd.DataFrame({
        "SourceName": raw.iloc[:, 0].astype(str).str.strip(),
        "SourceVED": raw[ved_col].fillna("").astype(str).str.strip(),
        "VEDClass": raw[ved_col].map(clean_ved),
    })
    source_rows = len(src)
    src = _match_source_names(src, dim_sku, alias_mapping)

    valid = src["VEDClass"].notna()
    matched = src[src["SKU_ID"].notna() & valid]
    quality = matched["MatchType"].map({"Exact name": 0, "Master key": 1, "Alias": 2})
    rank = matched["VEDClass"].map({v: i for i, v in enumerate(VED_CLASSES)})
    best = matched.assign(Quality=quality, Rank=rank).sort_values(["SKU_ID", "Quality", "Rank"]).drop_duplicates("SKU_ID")
    ved = best.set_index("SKU_ID")["VEDClass"]
    ved.index = ved.index.astype("int64")
    conflicts = matched.groupby("SKU_ID")["VEDClass"].nunique()
    conflict_ids = set(conflicts[conflicts > 1].index)

    unmatched = src[src["SKU_ID"].isna() & valid].assign(Issue="VED файлын нэр SKU-тэй таарсангүй")
    invalid = src[~valid].assign(Issue="VED утга V / E / D биш — хэрэглээгүй")
    conflict_rows = matched[matched["SKU_ID"].isin(conflict_ids)].assign(
        Issue="Олон VED — SKU-ийн өөрийн нэртэйг, үгүй бол хамгийн чухлыг (V > E > D) сонгов")
    uncovered = dim_sku.loc[~dim_sku["SKU_ID"].isin(ved.index), ["SKU_ID", "SKU_Name", "VED"]]
    uncovered = uncovered.rename(columns={"SKU_Name": "SourceName", "VED": "SourceVED"})
    uncovered = uncovered.assign(Issue="SKU VED файлд алга — одоогийн VED хэвээр")
    review = pd.concat([unmatched, invalid, conflict_rows, uncovered], ignore_index=True)
    review = review.rename(columns={"SourceVED": "VED"})[["Issue", "SourceName", "VED", "SKU_ID", "NormalizedKey", "MatchType"]]
    stats = {
        "source_rows": int(source_rows),
        "matched_rows": int(src.loc[src["SKU_ID"].notna(), "SourceName"].nunique()),
        "unmatched_rows": int(len(unmatched)),
        "invalid_rows": int(len(invalid)),
        "sku_covered": int(len(ved)),
        "sku_uncovered": int(len(uncovered)),
        "sku_conflicts": int(len(conflict_ids)),
    }
    return ved, review, stats


def apply_ved_map(dim_sku: pd.DataFrame, ved_by_sku: pd.Series) -> pd.DataFrame:
    """Give the SKUs listed in the VED file their VED and mark them in
    VED_Source. SKUs the file does not list keep their VED and source."""
    dim_sku = dim_sku.copy()
    if "VED_Source" not in dim_sku:
        dim_sku["VED_Source"] = VED_MASTER_SOURCE
    mapped = pd.to_numeric(dim_sku["SKU_ID"], errors="coerce").map(ved_by_sku)
    listed = mapped.notna()
    dim_sku.loc[listed, "VED"] = mapped[listed]
    dim_sku.loc[listed, "VED_Source"] = VED_FILE_SOURCE
    return dim_sku


def load_branch_list(path: str | Path) -> list[str]:
    """Branch names of «Салбарын жагсаалт.txt», in file order.

    The file is a quoted list ('АФ ... э/сан',); a file of plain lines, one
    name per line, works too. Spaces inside a name are kept as written."""
    text = Path(path).read_text(encoding="utf-8-sig")
    names = re.findall(r"'([^']*)'", text) or text.splitlines()
    names = [n.strip().strip(",").strip() for n in names]
    return list(dict.fromkeys(n for n in names if n))


# Cyrillic letters typed for the ABC / XYZ class letters.
_CLASS_LOOKALIKES = str.maketrans({"А": "A", "В": "B", "С": "C", "Х": "X", "У": "Y"})


def load_abc_xyz_file(
    path: str | Path,
    dim_sku: pd.DataFrame,
    dim_branch: pd.DataFrame,
    alias_mapping: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Branch ABC / XYZ classes of «ABC XYZ.xlsx»: product names down the
    first column, one column per branch, cells like «AZ».

    Names are matched like ``load_category_map``; branches on their name,
    exactly or by normalized key. Returns (SKU_ID, Branch_ID, ABC, XYZ) with
    one row per SKU x branch, and stats."""
    raw = pd.read_excel(path, sheet_name=0, header=0)
    name_col = raw.columns[0]
    names = pd.DataFrame({"SourceName": raw[name_col].astype(str).str.strip().drop_duplicates()})
    names = _match_source_names(names, dim_sku, alias_mapping)
    names = names[names["SKU_ID"].notna()]

    branch_ids = dict(zip(dim_branch["BranchName"].astype(str), dim_branch["Branch_ID"].astype(str)))
    by_key = {normalize_product_name(b): i for b, i in branch_ids.items()}
    columns = {c: branch_ids.get(str(c).strip()) or by_key.get(normalize_product_name(c)) for c in raw.columns[1:]}
    unmatched_branches = sorted(str(c) for c, i in columns.items() if i is None and not is_excluded_branch(c))
    columns = {c: i for c, i in columns.items() if i is not None and not is_excluded_branch(c)}

    cells = raw[[name_col, *columns]].rename(columns={name_col: "SourceName", **columns})
    cells["SourceName"] = cells["SourceName"].astype(str).str.strip()
    cells = cells.melt(id_vars="SourceName", var_name="Branch_ID", value_name="Class").dropna(subset=["Class"])
    cells["Class"] = cells["Class"].astype(str).str.strip().str.upper().str.translate(_CLASS_LOOKALIKES)
    valid = cells["Class"].str.fullmatch(r"[ABC][XYZ]")
    invalid_cells = int((~valid).sum())
    cells = cells[valid].merge(names[["SourceName", "SKU_ID", "MatchType"]], on="SourceName")
    # A SKU matched by its own name beats one matched through a shared key or alias.
    cells["Quality"] = cells["MatchType"].map({"Exact name": 0, "Master key": 1, "Alias": 2})
    cells = cells.sort_values(["SKU_ID", "Branch_ID", "Quality"]).drop_duplicates(["SKU_ID", "Branch_ID"])
    table = pd.DataFrame({
        "SKU_ID": cells["SKU_ID"].astype("int64"),
        "Branch_ID": cells["Branch_ID"].astype(str),
        "ABC": cells["Class"].str[0],
        "XYZ": cells["Class"].str[1],
    }).reset_index(drop=True)
    stats = {
        "source_file": Path(path).name,
        "source_names": int(raw[name_col].notna().sum()),
        "matched_names": int(names["SourceName"].nunique()),
        "branches_matched": len(columns),
        "branches_unmatched": unmatched_branches,
        "invalid_cells": invalid_cells,
        "rows": int(len(table)),
    }
    return table, stats


def load_project_data(data_dir: str | Path) -> dict[str, pd.DataFrame]:
    data_dir = Path(data_dir)
    frames = {}
    for name in [
        "dim_sku", "dim_branch", "dim_date", "dim_status", "dim_ved", "dim_category",
        "fact_branch_rop", "fact_inventory_snapshot", "fact_sku_status", "fact_status_history",
        "alias_mapping", "mapping_review",
    ]:
        parquet = data_dir / f"{name}.parquet"
        path_gz = data_dir / f"{name}.csv.gz"
        path_csv = data_dir / f"{name}.csv"
        if parquet.exists():
            frames[name] = pd.read_parquet(parquet)
        elif path_gz.exists():
            frames[name] = pd.read_csv(path_gz, low_memory=False)
        elif path_csv.exists():
            frames[name] = pd.read_csv(path_csv, low_memory=False)
    return frames


def dataframe_to_csv_data_uri(df: pd.DataFrame) -> str:
    csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
    encoded = base64.b64encode(csv_bytes).decode("ascii")
    return f"data:text/csv;base64,{encoded}"


def save_json(path: str | Path, payload: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
