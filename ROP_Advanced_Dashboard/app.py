from __future__ import annotations

import base64
import io
import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from bokeh.io import curdoc
from bokeh.layouts import column, gridplot, row
from bokeh.models import (
    Button,
    CheckboxGroup,
    ColorBar,
    ColumnDataSource,
    CustomJS,
    DataRange1d,
    DataTable,
    DatePicker,
    Div,
    FactorRange,
    FileInput,
    HoverTool,
    LinearAxis,
    LinearColorMapper,
    MultiChoice,
    NumberFormatter,
    Select,
    Slider,
    StringFormatter,
    TableColumn,
    TabPanel,
    Tabs,
    TextInput,
)
from bokeh.palettes import Category10, Viridis256
from bokeh.plotting import figure
from bokeh.transform import cumsum
from bokeh.util.hex import hexbin

from common import (
    STATUS_ORDER,
    VED_ORDER,
    build_status_snapshot,
    clean_category,
    is_excluded_branch,
    load_project_data,
    map_inventory,
    parse_inventory,
)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SNAPSHOT_DIR = BASE_DIR / "snapshots"
SNAPSHOT_DIR.mkdir(exist_ok=True)

frames = load_project_data(DATA_DIR)
dim_sku = frames["dim_sku"].copy()
dim_branch = frames["dim_branch"].copy()
fact_branch_rop = frames["fact_branch_rop"].copy()
alias_mapping = frames["alias_mapping"].copy()
current_inventory = frames.get("fact_inventory_snapshot", pd.DataFrame()).copy()
current_status = frames.get("fact_sku_status", pd.DataFrame()).copy()
current_mapping_review = frames.get("mapping_review", pd.DataFrame()).copy()
metadata = json.loads((DATA_DIR / "metadata.json").read_text(encoding="utf-8"))

# Normalize loaded dtypes.
dim_sku["Category"] = dim_sku["Category"].map(clean_category)
fact_branch_rop["Category"] = fact_branch_rop["Category"].map(clean_category)
current_status["Category"] = current_status["Category"].map(clean_category)
dim_branch = dim_branch.loc[~dim_branch["BranchName"].map(is_excluded_branch)].copy()
fact_branch_rop = fact_branch_rop.loc[~fact_branch_rop["BranchName"].map(is_excluded_branch)].copy()
current_inventory = current_inventory.loc[~current_inventory["BranchName"].map(is_excluded_branch)].copy()
current_status = current_status.loc[~current_status["BranchName"].map(is_excluded_branch)].copy()
for frame in [dim_sku, fact_branch_rop, current_inventory, current_status]:
    if "SKU_ID" in frame:
        frame["SKU_ID"] = pd.to_numeric(frame["SKU_ID"], errors="coerce").astype("Int64")
for col in ["OnHand", "OnOrder", "Backorder", "InventoryPosition", "ROP", "ROPGap", "CoverageRatio", "WeightedGap", "PriorityScore"]:
    if col in current_status:
        current_status[col] = pd.to_numeric(current_status[col], errors="coerce")

STATUS_COLORS = {
    "Тасарсан": "#B91C1C",
    "ROP-оос доош": "#F97316",
    "Өгөгдөл алга": "#7C3AED",
    "ROP байхгүй": "#6B7280",
    "Хэвийн": "#0F766E",
    "Илүүдэл": "#2563EB",
}
VED_COLORS = {"V": "#B91C1C", "E": "#F59E0B", "D": "#0F766E", "Тодорхойгүй": "#6B7280"}
HEX_SIZE = 0.12  # bin width in log10 units for the ROP-vs-inventory density plot

state = {
    "inventory": current_inventory,
    "status": current_status,
    "mapping_review": current_mapping_review,
    "scope": str(current_status["Scope"].dropna().iloc[0]) if len(current_status) and "Scope" in current_status else "NETWORK",
    "source_file": metadata.get("source_files", {}).get("inventory", "Initial snapshot"),
    "mapping_stats": metadata.get("mapping", {}),
}

TITLE_CSS = """
<div style="font-family:Segoe UI,Arial,sans-serif;background:linear-gradient(90deg,#12263A,#1F4E5F);color:white;padding:18px 24px;border-radius:12px;">
  <div style="font-size:26px;font-weight:700;">ROP & Нөөцийн удирдлагын ахисан түвшний дашбоард</div>
  <div style="font-size:13px;opacity:.88;margin-top:4px;">Python ETL + интерактив хяналт + Power BI-ready star schema</div>
</div>
"""
header = Div(text=TITLE_CSS, sizing_mode="stretch_width", height=92)
freshness_banner = Div(text="", sizing_mode="stretch_width", height=44)

upload = FileInput(accept=".xlsx,.xlsb,.csv", width=330)
snapshot_picker = DatePicker(title="Үлдэгдлийн огноо", value=metadata.get("snapshot_date", date.today().isoformat()), width=170)
excess_slider = Slider(title="Илүүдлийн босго (ROP ×)", start=1.1, end=5.0, value=float(metadata.get("excess_threshold", 2.0)), step=0.1, width=240)
missing_as_zero = CheckboxGroup(labels=["Мэдээлэлгүй SKU-г 0 үлдэгдэл гэж үзэх"], active=[], width=280)
reset_button = Button(label="Эхний snapshot сэргээх", button_type="default", width=180)
clear_filters_button = Button(label="Шүүлтүүр цэвэрлэх", button_type="default", width=160)
critical_only = CheckboxGroup(labels=["Зөвхөн эрсдэлтэй (Тасарсан / ROP-оос доош)"], active=[], width=320)
message = Div(text="", width=600, height=55)

status_filter = MultiChoice(title="Status", value=[], options=STATUS_ORDER, width=260)
ved_filter = MultiChoice(title="VED", value=[], options=VED_ORDER, width=220)
category_options = sorted(str(x) for x in dim_sku["Category"].dropna().unique())
category_filter = MultiChoice(title="Категори", value=[], options=category_options, width=260)
confidence_filter = MultiChoice(title="ROP confidence", value=[], options=["Өндөр", "Бага"], width=200)
branch_filter = Select(title="Салбар", value="ALL", options=["ALL"] + sorted(str(x) for x in current_status.get("BranchName", pd.Series(["NETWORK"])).dropna().unique()), width=300)
search_input = TextInput(title="Барааны нэр хайх", placeholder="жишээ: Тобрекс", width=320)
min_gap_slider = Slider(title="Доод ROP gap", start=0, end=max(1000, float(current_status.get("ROPGap", pd.Series([0])).max(skipna=True) or 0)), value=0, step=1, width=240)

kpi_container = Div(text="", sizing_mode="stretch_width", height=135)
status_source = ColumnDataSource(data=dict(Status=[], Count=[], Angle=[], Color=[]))
ved_source = ColumnDataSource(data=dict(VED=[], Critical=[], Gap=[], Color=[]))
top_source = ColumnDataSource(data=dict(Name=[], Label=[], Branch=[], Gap=[], WeightedGap=[], VED=[], Status=[], OnHand=[], ROP=[], Color=[]))
scatter_source = ColumnDataSource(data=dict(Name=[], ROPPlot=[], InvPlot=[], ROP=[], Inventory=[], Gap=[], VED=[], Status=[], Color=[]))
hex_source = ColumnDataSource(data=dict(q=[], r=[], counts=[]))
branch_source = ColumnDataSource(data=dict(Branch=[], ROP=[], VROP=[], EROP=[], DROP=[], Color=[]))
coverage_source = ColumnDataSource(data=dict(
    Branch=[], Category=[], Coverage=[], SKUCount=[], CoveredSKU=[], ROP=[], Inventory=[], Gap=[],
))
quality_source = ColumnDataSource(data=dict(Metric=[], Value=[], Label=[], Color=[]))
table_source = ColumnDataSource(data=dict())
review_source = ColumnDataSource(data=dict())


def empty_figure(title: str, height: int = 360):
    p = figure(title=title, height=height, sizing_mode="stretch_width", toolbar_location="above")
    p.grid.grid_line_alpha = 0.15
    p.title.text_font_size = "14pt"
    p.title.text_font_style = "bold"
    return p


status_plot = empty_figure("Status бүтэц", 370)
status_plot.annular_wedge(
    x=0, y=0, inner_radius=0.55, outer_radius=0.95,
    start_angle=cumsum("Angle", include_zero=True), end_angle=cumsum("Angle"),
    color="Color", legend_field="Status", source=status_source,
)
status_plot.axis.visible = False
status_plot.grid.grid_line_color = None
status_plot.legend.location = "center_right"
status_plot.legend.label_text_font_size = "9pt"
status_plot.add_tools(HoverTool(tooltips=[("Status", "@Status"), ("SKU", "@Count{0,0}")]))

ved_plot = figure(
    title="VED эрсдэл: critical SKU ба ROP gap", height=370, sizing_mode="stretch_width",
    toolbar_location="above", x_range=FactorRange(*VED_ORDER),
)
ved_plot.grid.grid_line_alpha = 0.15
ved_plot.title.text_font_size = "14pt"
ved_plot.title.text_font_style = "bold"
ved_bar_renderer = ved_plot.vbar(x="VED", top="Critical", width=0.55, color="Color", source=ved_source, legend_label="Critical SKU")
ved_plot.extra_y_ranges = {"gap": DataRange1d()}
ved_plot.add_layout(LinearAxis(y_range_name="gap", axis_label="ROP gap"), "right")
ved_gap_renderer = ved_plot.scatter(x="VED", y="Gap", y_range_name="gap", source=ved_source, size=12, marker="diamond", color="#111827", legend_label="ROP gap")
# Bokeh's DataRange1d auto-scales from every renderer on the plot by
# default, regardless of which axis they're assigned to -- without scoping
# each range explicitly, the primary (Critical count, max ~hundreds) axis
# balloons to match the secondary (ROP gap, max ~tens of thousands) axis,
# squashing the bars to invisible slivers.
ved_plot.y_range.renderers = [ved_bar_renderer]
ved_plot.extra_y_ranges["gap"].renderers = [ved_gap_renderer]
ved_plot.add_tools(HoverTool(tooltips=[("VED", "@VED"), ("Critical SKU", "@Critical{0,0}"), ("ROP gap", "@Gap{0,0}")]))
ved_plot.xgrid.grid_line_color = None
ved_plot.y_range.start = 0
ved_plot.legend.location = "top_left"
ved_plot.legend.label_text_font_size = "9pt"
ved_plot.legend.background_fill_alpha = 0.7

shortage_plot = figure(
    title="Нөхөх шаардлагатай TOP 20 SKU",
    y_range=[], height=520, sizing_mode="stretch_width", toolbar_location="above",
)
shortage_plot.hbar(y="Label", right="WeightedGap", height=0.72, color="Color", source=top_source)
shortage_plot.add_tools(HoverTool(tooltips=[
    ("Нэр", "@Name"), ("Салбар", "@Branch"), ("VED", "@VED"), ("Status", "@Status"),
    ("On hand", "@OnHand{0,0.00}"), ("ROP", "@ROP{0,0}"), ("Gap", "@Gap{0,0.00}"),
]))
shortage_plot.xaxis.axis_label = "VED-weighted ROP gap"
shortage_plot.grid.grid_line_alpha = 0.15

scatter_plot = figure(
    title="Inventory Position vs ROP — нягтралын дулааны зураг", height=500,
    sizing_mode="stretch_width", toolbar_location="above",
)
# Density layer: every SKU gets binned into a hexagon instead of drawn as its
# own dot, so thousands of points read as a heatmap rather than a haze.
hex_color_mapper = LinearColorMapper(palette=Viridis256, low=0, high=1)
hex_renderer = scatter_plot.hex_tile(
    q="q", r="r", size=HEX_SIZE, source=hex_source, line_color=None,
    fill_color={"field": "counts", "transform": hex_color_mapper},
)
scatter_plot.add_tools(HoverTool(tooltips=[("SKU тоо (нүд бүрт)", "@counts")], renderers=[hex_renderer]))
scatter_plot.add_layout(
    ColorBar(color_mapper=hex_color_mapper, label_standoff=8, title="SKU тоо", location=(0, 0)),
    "right",
)
# Point layer: only actionable SKUs (stocked-out / below ROP) get an
# individual, hoverable dot drawn on top of the density.
critical_renderer = scatter_plot.scatter(
    x="ROPPlot", y="InvPlot", size=8, alpha=0.9, color="Color",
    line_color="#111827", line_width=0.5, source=scatter_source,
    legend_label="Эрсдэлтэй SKU (Тасарсан / ROP-оос доош)",
)
scatter_plot.add_tools(HoverTool(tooltips=[
    ("Нэр", "@Name"), ("VED", "@VED"), ("Status", "@Status"),
    ("ROP", "@ROP{0,0}"), ("Inventory", "@Inventory{0,0.00}"), ("Gap", "@Gap{0,0.00}"),
], renderers=[critical_renderer]))
scatter_plot.line([0, 6], [0, 6], line_dash="dashed", line_color="#4B5563", line_width=1)
scatter_plot.xaxis.axis_label = "log10(ROP + 1)"
scatter_plot.yaxis.axis_label = "log10(Inventory Position + 1)"
scatter_plot.legend.location = "top_left"
scatter_plot.legend.background_fill_alpha = 0.7

branch_plot = figure(title="Салбарын ROP exposure", y_range=[], height=610, sizing_mode="stretch_width", toolbar_location="above")
branch_plot.hbar_stack(["VROP", "EROP", "DROP"], y="Branch", height=0.74, color=[VED_COLORS["V"], VED_COLORS["E"], VED_COLORS["D"]], source=branch_source, legend_label=["V", "E", "D"])
branch_plot.add_tools(HoverTool(tooltips=[("Салбар", "@Branch"), ("Total ROP", "@ROP{0,0}"), ("V", "@VROP{0,0}"), ("E", "@EROP{0,0}"), ("D", "@DROP{0,0}")]))
branch_plot.legend.location = "bottom_right"
branch_plot.xaxis.axis_label = "Corrected ROP"

coverage_mapper = LinearColorMapper(palette=Viridis256, low=0, high=1)
coverage_plot = figure(
    title="SKU coverage heatmap — салбар × категори",
    x_range=[], y_range=[], height=650, sizing_mode="stretch_width",
    toolbar_location="above",
)
coverage_plot.rect(
    x="Category", y="Branch", width=1, height=1, source=coverage_source,
    fill_color={"field": "Coverage", "transform": coverage_mapper},
    line_color="#FFFFFF", line_width=0.5,
)
coverage_plot.add_tools(HoverTool(tooltips=[
    ("Салбар", "@Branch"),
    ("Категори", "@Category"),
    ("Coverage", "@Coverage{0.0%}"),
    ("Covered SKU", "@CoveredSKU{0,0} / @SKUCount{0,0}"),
    ("ROP", "@ROP{0,0}"),
    ("Inventory", "@Inventory{0,0}"),
    ("Gap", "@Gap{0,0}"),
]))
coverage_plot.add_layout(ColorBar(
    color_mapper=coverage_mapper, label_standoff=8, title="SKU coverage", location=(0, 0),
), "right")
coverage_plot.xaxis.major_label_orientation = 0.8
coverage_plot.xaxis.axis_label = "Категори"
coverage_plot.yaxis.axis_label = "Салбар"
coverage_plot.grid.grid_line_color = None

coverage_columns = [
    TableColumn(field="Branch", title="Салбар", width=220),
    TableColumn(field="Category", title="Категори", width=180),
    TableColumn(field="SKUCount", title="SKU", formatter=NumberFormatter(format="0,0"), width=75),
    TableColumn(field="CoveredSKU", title="Covered SKU", formatter=NumberFormatter(format="0,0"), width=105),
    TableColumn(field="Coverage", title="Coverage", formatter=NumberFormatter(format="0.0%"), width=95),
    TableColumn(field="ROP", title="ROP", formatter=NumberFormatter(format="0,0"), width=95),
    TableColumn(field="Inventory", title="Inventory", formatter=NumberFormatter(format="0,0"), width=110),
    TableColumn(field="Gap", title="Gap", formatter=NumberFormatter(format="0,0"), width=100),
]
coverage_table = DataTable(
    source=coverage_source, columns=coverage_columns, height=520,
    sizing_mode="stretch_width", index_position=None,
)

quality_plot = figure(title="Өгөгдлийн чанарын KPI", x_range=[], height=410, sizing_mode="stretch_width", toolbar_location=None)
quality_plot.vbar(x="Metric", top="Value", width=0.68, color="Color", source=quality_source)
quality_plot.text(x="Metric", y="Value", text="Label", source=quality_source, text_align="center", text_baseline="bottom", text_font_size="10pt")
quality_plot.xaxis.major_label_orientation = 0.8
quality_plot.y_range.start = 0
quality_plot.grid.grid_line_alpha = 0.15

main_columns = [
    TableColumn(field="SKU_ID", title="SKU ID", formatter=NumberFormatter(format="0"), width=70),
    TableColumn(field="SKU_Name", title="Нэр төрөл", formatter=StringFormatter(), width=330),
    TableColumn(field="BranchName", title="Салбар", width=230),
    TableColumn(field="VED", title="VED", width=55),
    TableColumn(field="Category", title="Категори", width=90),
    TableColumn(field="Status", title="Status", width=115),
    TableColumn(field="InventoryPosition", title="Inventory", formatter=NumberFormatter(format="0,0.00"), width=95),
    TableColumn(field="ROP", title="ROP", formatter=NumberFormatter(format="0,0"), width=80),
    TableColumn(field="ROPGap", title="Gap", formatter=NumberFormatter(format="0,0.00"), width=90),
    TableColumn(field="CoverageRatio", title="Coverage", formatter=NumberFormatter(format="0.0%"), width=90),
    TableColumn(field="ROP_Confidence", title="Confidence", width=85),
    TableColumn(field="Supplier", title="Нийлүүлэгч", width=220),
]
main_table = DataTable(source=table_source, columns=main_columns, height=540, sizing_mode="stretch_width", index_position=None, selectable=True)
detail_panel = Div(text="", sizing_mode="stretch_width")

review_columns = [
    TableColumn(field="InventoryName", title="Inventory name", width=330),
    TableColumn(field="OnHand", title="On hand", formatter=NumberFormatter(format="0,0.00"), width=90),
    TableColumn(field="BranchName", title="Branch", width=200),
    TableColumn(field="Suggested_SKU_ID", title="Suggested ID", width=90),
    TableColumn(field="Suggested_SKU_Name", title="Suggested name", width=330),
    TableColumn(field="Similarity", title="Similarity", formatter=NumberFormatter(format="0.0"), width=90),
    TableColumn(field="Decision", title="Decision", width=120),
]
review_table = DataTable(source=review_source, columns=review_columns, height=570, sizing_mode="stretch_width", index_position=None, selectable=True)
accept_button = Button(label="Зөвшөөрөх (alias нэмэх)", button_type="success", width=220)
reject_button = Button(label="Татгалзах", button_type="default", width=120)
review_message = Div(text="", width=520, height=30)

for _c in main_table.columns + review_table.columns:
    _c.sortable = True

methodology = Div(text="""
<div style="font-family:Segoe UI,Arial,sans-serif;line-height:1.55;padding:16px 20px;background:white;border-radius:10px;border:1px solid #E5E7EB;">
<h2 style="color:#12263A;">ROP аргачлал</h2>
<p><b>Corrected ROP</b> = CEILING(AvgDemand × MeanLT + Z<sub>final</sub> × √(MeanLT × DemandSD² + AvgDemand² × LTSD²), 1)</p>
<ul>
<li>Z<sub>final</sub> = MAX(Z VED, Z ABC–XYZ)</li>
<li>Inventory Position = On Hand + On Order − Backorder</li>
<li>Status: тасарсан, ROP-оос доош, хэвийн, илүүдэл, өгөгдөл алга, ROP байхгүй</li>
<li>Дэлгэрэнгүй ROP байхгүй SKU-д fallback ROP хадгалсан бөгөөд confidence = “Бага”.</li>
</ul>
<h3 style="color:#12263A;">Автомат шинэчлэл</h3>
<p>Үлдэгдлийн XLSX/XLSB/CSV файлыг дээрх upload хэсэгт оруулахад Python ETL нэр, SKU ID, салбар, үлдэгдлийн багануудыг таньж, alias mapping ашиглан холбож, статус болон бүх графикийг шинэчилнэ.</p>
</div>
""", sizing_mode="stretch_width")


def card(label: str, value: str, subtitle: str, accent: str) -> str:
    return f"""
    <div style="flex:1 1 0;min-width:170px;background:white;border:1px solid #E5E7EB;border-top:4px solid {accent};border-radius:10px;padding:14px 16px;box-shadow:0 1px 3px rgba(0,0,0,.05);">
      <div style="font-size:11.5px;color:#6B7280;font-weight:600;letter-spacing:.03em;text-transform:uppercase;">{label}</div>
      <div style="font-size:26px;color:#111827;font-weight:750;margin-top:5px;">{value}</div>
      <div style="font-size:11.5px;color:#6B7280;margin-top:4px;">{subtitle}</div>
    </div>"""


def filtered_status() -> pd.DataFrame:
    df = state["status"].copy()
    if status_filter.value:
        df = df[df["Status"].isin(status_filter.value)]
    if ved_filter.value:
        df = df[df["VED"].isin(ved_filter.value)]
    if category_filter.value:
        df = df[df["Category"].isin(category_filter.value)]
    if confidence_filter.value:
        df = df[df["ROP_Confidence"].isin(confidence_filter.value)]
    if branch_filter.value != "ALL" and "BranchName" in df:
        df = df[df["BranchName"] == branch_filter.value]
    if critical_only.active:
        df = df[df["Status"].isin(["Тасарсан", "ROP-оос доош"])]
    if search_input.value.strip():
        term = search_input.value.strip().casefold()
        mask = df["SKU_Name"].astype(str).str.casefold().str.contains(term, regex=False)
        for col in ["SKU_ID", "Supplier", "Manufacturer", "BranchName"]:
            if col in df:
                mask |= df[col].astype(str).str.casefold().str.contains(term, regex=False)
        df = df[mask]
    if min_gap_slider.value > 0:
        df = df[df["ROPGap"].fillna(0) >= min_gap_slider.value]
    return df


def update_kpis(df: pd.DataFrame) -> None:
    total_sku = df["SKU_ID"].nunique() if len(df) else 0
    on_hand = df["InventoryPosition"].sum(skipna=True) if len(df) else 0
    total_rop = df["ROP"].sum(skipna=True) if len(df) else 0
    gap = df["ROPGap"].sum(skipna=True) if len(df) else 0
    critical = int(df.get("IsCritical", pd.Series(dtype=bool)).fillna(False).sum()) if len(df) else 0
    mapping_rate = state.get("mapping_stats", {}).get("mapping_rate_rows", 0)
    html = '<div style="display:flex;gap:12px;flex-wrap:wrap;width:100%;box-sizing:border-box;font-family:Segoe UI,Arial,sans-serif;">'
    html += card("Шүүсэн SKU", f"{total_sku:,.0f}", f"Scope: {state['scope']}", "#1F4E5F")
    html += card("Inventory Position", f"{on_hand:,.0f}", "Matched inventory", "#2563EB")
    html += card("Total ROP", f"{total_rop:,.0f}", "Ашиглах ROP", "#0F766E")
    html += card("ROP gap", f"{gap:,.0f}", "Нөхөх шаардлагатай", "#F97316")
    html += card("Critical V/E", f"{critical:,.0f}", "Тасарсан эсвэл ROP-оос доош", "#B91C1C")
    html += card("Mapping rate", f"{mapping_rate:.1%}", state.get("source_file", ""), "#7C3AED")
    html += "</div>"
    kpi_container.text = html


def update_freshness() -> None:
    snapshot = str(snapshot_picker.value or metadata.get("snapshot_date", ""))
    src = state.get("source_file", "")
    mapping = state.get("mapping_stats", {})
    rate = mapping.get("mapping_rate_rows", 0)
    freshness_banner.text = (
        '<div style="display:flex;gap:18px;align-items:center;flex-wrap:wrap;'
        'font-family:Segoe UI,Arial,sans-serif;font-size:12.5px;color:#374151;'
        'padding:8px 14px;background:#F3F4F6;border-radius:8px;margin-top:8px;">'
        f'<span><b style="color:#12263A;">Сүүлийн шинэчлэл:</b> {snapshot}</span>'
        f'<span><b style="color:#12263A;">Эх файл:</b> {src}</span>'
        f'<span><b style="color:#12263A;">Mapping:</b> {rate:.1%}</span>'
        f'<span><b style="color:#12263A;">Scope:</b> {state["scope"]}</span>'
        '</div>'
    )


def update_status_chart(df: pd.DataFrame) -> None:
    counts = df["Status"].value_counts().reindex(STATUS_ORDER, fill_value=0)
    total = max(int(counts.sum()), 1)
    status_source.data = {
        "Status": list(counts.index),
        "Count": counts.astype(int).tolist(),
        "Angle": (counts / total * 2 * np.pi).tolist(),
        "Color": [STATUS_COLORS[s] for s in counts.index],
    }


def update_ved_chart(df: pd.DataFrame) -> None:
    grouped = []
    for ved in VED_ORDER:
        sub = df[df["VED"] == ved]
        critical = int(sub["Status"].isin(["Тасарсан", "ROP-оос доош"]).sum())
        gap = float(sub["ROPGap"].sum(skipna=True))
        grouped.append((ved, critical, gap, VED_COLORS[ved]))
    ved_source.data = {
        "VED": [x[0] for x in grouped],
        "Critical": [x[1] for x in grouped],
        "Gap": [x[2] for x in grouped],
        "Color": [x[3] for x in grouped],
    }


def _unique_bar_labels(names: list[str], hints: list[str]) -> list[str]:
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


def update_top_chart(df: pd.DataFrame) -> None:
    risk = df[df["Status"].isin(["Тасарсан", "ROP-оос доош"])].copy()
    risk = risk.sort_values(["WeightedGap", "PriorityScore"], ascending=False).head(20)
    names = [str(x)[:55] for x in risk["SKU_Name"]]
    branches = risk["BranchName"].astype(str).tolist() if "BranchName" in risk.columns else [""] * len(risk)
    labels = _unique_bar_labels(names, branches)
    top_source.data = {
        "Name": names,
        "Label": labels,
        "Branch": branches,
        "Gap": risk["ROPGap"].fillna(0).tolist(),
        "WeightedGap": risk["WeightedGap"].fillna(0).tolist(),
        "VED": risk["VED"].fillna("Тодорхойгүй").tolist(),
        "Status": risk["Status"].fillna("").tolist(),
        "OnHand": risk["InventoryPosition"].fillna(0).tolist(),
        "ROP": risk["ROP"].fillna(0).tolist(),
        "Color": [VED_COLORS.get(v, "#6B7280") for v in risk["VED"].fillna("Тодорхойгүй")],
    }
    shortage_plot.y_range.factors = labels[::-1]


def update_scatter(df: pd.DataFrame) -> None:
    plot_df = df[df["ROP"].fillna(0) > 0].copy()

    # Density layer: bin every plotted SKU into hexagons. hexbin() is cheap
    # even at 50k+ rows, so there's no need to sample the population down.
    if len(plot_df):
        log_rop = np.log10(plot_df["ROP"].fillna(0).clip(lower=0) + 1).to_numpy()
        log_inv = np.log10(plot_df["InventoryPosition"].fillna(0).clip(lower=0) + 1).to_numpy()
        bins = hexbin(log_rop, log_inv, size=HEX_SIZE)
        # Bokeh >=3.9 returns a dataclass (bins.q); older versions return a
        # DataFrame (bins["q"]) — support both.
        q, r, counts = (bins.q, bins.r, bins.counts) if hasattr(bins, "q") else (bins["q"], bins["r"], bins["counts"])
        hex_source.data = {"q": list(q), "r": list(r), "counts": list(counts)}
        hex_color_mapper.high = max(int(np.max(counts)), 1)
    else:
        hex_source.data = {"q": [], "r": [], "counts": []}
        hex_color_mapper.high = 1

    # Point layer: only stocked-out / below-ROP SKUs are drawn individually,
    # since those are the ones a planner actually needs to click into. Cap
    # as a safety net in the (unusual) case almost everything is critical.
    risk = plot_df[plot_df["Status"].isin(["Тасарсан", "ROP-оос доош"])].copy()
    if len(risk) > 3000:
        risk = risk.nlargest(3000, "PriorityScore")
    inv = risk["InventoryPosition"].fillna(0).clip(lower=0)
    rop = risk["ROP"].fillna(0).clip(lower=0)
    scatter_source.data = {
        "Name": risk["SKU_Name"].astype(str).tolist(),
        "ROPPlot": np.log10(rop + 1).tolist(),
        "InvPlot": np.log10(inv + 1).tolist(),
        "ROP": rop.tolist(),
        "Inventory": inv.tolist(),
        "Gap": risk["ROPGap"].fillna(0).tolist(),
        "VED": risk["VED"].fillna("Тодорхойгүй").tolist(),
        "Status": risk["Status"].fillna("").tolist(),
        "Color": [STATUS_COLORS.get(s, "#6B7280") for s in risk["Status"]],
    }


def update_branch_chart() -> None:
    br = fact_branch_rop.copy()
    group = br.groupby("BranchName", as_index=False).agg(
        ROP=("ROP", "sum"),
        VROP=("ROP", lambda x: float(x[br.loc[x.index, "VED"] == "V"].sum())),
        EROP=("ROP", lambda x: float(x[br.loc[x.index, "VED"] == "E"].sum())),
        DROP=("ROP", lambda x: float(x[br.loc[x.index, "VED"] == "D"].sum())),
    )
    group = group.sort_values("ROP", ascending=False).head(30)
    labels = group["BranchName"].astype(str).tolist()
    branch_source.data = {
        "Branch": labels,
        "ROP": group["ROP"].tolist(),
        "VROP": group["VROP"].tolist(),
        "EROP": group["EROP"].tolist(),
        "DROP": group["DROP"].tolist(),
        "Color": ["#1F4E5F"] * len(group),
    }
    branch_plot.y_range.factors = labels[::-1]


def update_coverage(df: pd.DataFrame) -> None:
    if df.empty:
        coverage_source.data = {key: [] for key in coverage_source.data}
        coverage_plot.x_range.factors = []
        coverage_plot.y_range.factors = []
        return

    work = df.copy()
    work["BranchName"] = work["BranchName"].fillna("Тодорхойгүй").astype(str)
    work["Category"] = work["Category"].map(clean_category)
    work["ROP"] = pd.to_numeric(work["ROP"], errors="coerce").fillna(0)
    work["InventoryPosition"] = pd.to_numeric(work["InventoryPosition"], errors="coerce").fillna(0)
    work["HasInventory"] = work.get("HasInventory", pd.Series(True, index=work.index)).fillna(False).astype(bool)
    eligible = work["ROP"] > 0
    coverage_ratio = np.where(
        eligible & work["HasInventory"],
        (work["InventoryPosition"] / work["ROP"]).clip(lower=0, upper=1),
        0,
    )
    covered = eligible & work["HasInventory"] & (coverage_ratio >= 1)
    work["_Eligible"] = eligible
    work["_Covered"] = covered
    work["_CoverageRatio"] = coverage_ratio
    grouped = work.groupby(["BranchName", "Category"], as_index=False).agg(
        SKUCount=("SKU_ID", "nunique"),
        CoveredSKU=("_Covered", "sum"),
        EligibleSKU=("_Eligible", "sum"),
        CoverageRatio=("_CoverageRatio", "sum"),
        ROP=("ROP", "sum"),
        Inventory=("InventoryPosition", "sum"),
    )
    grouped["Coverage"] = np.where(
        grouped["EligibleSKU"] > 0,
        grouped["CoverageRatio"] / grouped["EligibleSKU"],
        np.nan,
    )
    grouped["Gap"] = (grouped["ROP"] - grouped["Inventory"]).clip(lower=0)
    grouped = grouped.sort_values(["BranchName", "Category"])
    coverage_source.data = {
        "Branch": grouped["BranchName"].tolist(),
        "Category": grouped["Category"].tolist(),
        "Coverage": grouped["Coverage"].fillna(0).tolist(),
        "SKUCount": grouped["SKUCount"].astype(int).tolist(),
        "CoveredSKU": grouped["CoveredSKU"].astype(int).tolist(),
        "ROP": grouped["ROP"].tolist(),
        "Inventory": grouped["Inventory"].tolist(),
        "Gap": grouped["Gap"].tolist(),
    }
    categories = sorted(grouped["Category"].unique().tolist())
    branches = grouped.groupby("BranchName")["ROP"].sum().sort_values(ascending=True).index.tolist()
    coverage_mapper.low = 0
    coverage_mapper.high = 1
    coverage_plot.x_range.factors = categories
    coverage_plot.y_range.factors = branches


def update_quality() -> None:
    st = state["status"]
    inv = state["inventory"]
    mapping = state.get("mapping_stats", {})
    metrics = [
        ("Matched rows", mapping.get("matched_rows", 0), f"{mapping.get('mapping_rate_rows', 0):.1%}", "#0F766E"),
        ("Unmatched rows", mapping.get("unmatched_rows", 0), f"{mapping.get('unmatched_rows', 0):,.0f}", "#F97316"),
        ("Missing inventory SKU", int((~st["HasInventory"].fillna(False)).sum()) if "HasInventory" in st else 0, "SKU", "#7C3AED"),
        ("Missing ROP SKU", int((~dim_sku["HasROP"].astype(bool)).sum()), "SKU", "#6B7280"),
        ("Low-confidence ROP", int((dim_sku["ROP_Confidence"] == "Бага").sum()), "SKU", "#F59E0B"),
        ("Negative inventory", int((pd.to_numeric(inv.get("OnHand", 0), errors="coerce") < 0).sum()) if len(inv) else 0, "rows", "#B91C1C"),
    ]
    quality_source.data = {
        "Metric": [m[0] for m in metrics],
        "Value": [m[1] for m in metrics],
        "Label": [m[2] for m in metrics],
        "Color": [m[3] for m in metrics],
    }
    quality_plot.x_range.factors = [m[0] for m in metrics]


def update_table(df: pd.DataFrame) -> None:
    cols = [
        "SKU_ID", "SKU_Name", "BranchName", "VED", "Category", "Status", "InventoryPosition", "ROP", "ROPGap",
        "CoverageRatio", "ROP_Confidence", "Supplier",
    ]
    table_df = df.sort_values(["PriorityScore", "WeightedGap"], ascending=False).head(1000).copy()
    for col in cols:
        if col not in table_df:
            table_df[col] = np.nan
    table_source.data = ColumnDataSource.from_df(table_df[cols])


def update_review_table() -> None:
    review = state["mapping_review"].copy()
    cols = ["InventoryName", "OnHand", "BranchName", "Suggested_SKU_ID", "Suggested_SKU_Name", "Similarity", "Decision", "NormalizedKey"]
    for col in cols:
        if col not in review:
            review[col] = np.nan
    review_source.data = ColumnDataSource.from_df(review[cols].head(2000))


def refresh_all() -> None:
    df = filtered_status()
    update_kpis(df)
    update_status_chart(df)
    update_ved_chart(df)
    update_top_chart(df)
    update_scatter(df)
    update_table(df)
    update_coverage(df)
    update_quality()
    update_review_table()
    update_freshness()


def rebuild_from_inventory(inv_df: pd.DataFrame, source_file: str, mapping_review: pd.DataFrame | None = None, mapping_stats: dict | None = None) -> None:
    date_value = snapshot_picker.value or metadata.get("snapshot_date", date.today().isoformat())
    status = build_status_snapshot(dim_sku, inv_df, fact_branch_rop, date_value, excess_slider.value)
    if missing_as_zero.active:
        missing_mask = ~status["HasInventory"]
        status.loc[missing_mask, ["OnHand", "OnOrder", "Backorder", "InventoryPosition"]] = 0.0
        status.loc[missing_mask, "ROPGap"] = status.loc[missing_mask, "ROP"].clip(lower=0)
        status.loc[missing_mask, "CoverageRatio"] = np.where(status.loc[missing_mask, "ROP"] > 0, 0.0, np.nan)
        status.loc[missing_mask & (status["ROP"] > 0), "Status"] = "Тасарсан"
        status.loc[missing_mask & (status["ROP"] <= 0), "Status"] = "ROP байхгүй"
        status.loc[missing_mask, "HasInventory"] = True
        status.loc[missing_mask, "DataMatch"] = "Missing treated as zero"
    state["inventory"] = inv_df
    state["status"] = status
    state["mapping_review"] = mapping_review if mapping_review is not None else pd.DataFrame()
    state["mapping_stats"] = mapping_stats or {}
    state["scope"] = str(status["Scope"].iloc[0]) if len(status) else "NETWORK"
    state["source_file"] = source_file
    branch_filter.options = ["ALL"] + sorted(status["BranchName"].dropna().astype(str).unique().tolist())
    branch_filter.value = "ALL"
    min_gap_slider.end = max(1000, float(status["ROPGap"].max(skipna=True) or 0))
    refresh_all()


def friendly_error(exc: Exception) -> str:
    msg = str(exc)
    known = [
        "Тохирох үлдэгдлийн хүснэгт олдсонгүй",
        "Үлдэгдлийн багануудыг таньж чадсангүй",
        "SKU ID эсвэл Нэр төрөл багана олдсонгүй",
        "Үлдэгдэл / Эцсийн үлдэгдэл багана олдсонгүй",
    ]
    for k in known:
        if k in msg:
            return k
    return f"{type(exc).__name__}: {msg}"


def upload_callback(attr: str, old: str, new: str) -> None:
    if not new:
        return
    try:
        payload = base64.b64decode(new)
        inventory, parse_meta = parse_inventory(payload, filename=upload.filename)
        inventory["SnapshotDate"] = pd.to_datetime(inventory["SnapshotDate"], errors="coerce").fillna(pd.Timestamp(snapshot_picker.value))
        inventory["SourceFile"] = upload.filename
        mapped, review, stats = map_inventory(inventory, alias_mapping, dim_sku, dim_branch)
        rebuild_from_inventory(mapped, upload.filename, review, stats)
        stamp = str(snapshot_picker.value).replace("-", "")
        mapped.to_csv(SNAPSHOT_DIR / f"inventory_{stamp}.csv.gz", index=False, encoding="utf-8-sig", compression="gzip")
        state["status"].to_csv(SNAPSHOT_DIR / f"status_{stamp}.csv.gz", index=False, encoding="utf-8-sig", compression="gzip")
        review.to_csv(SNAPSHOT_DIR / f"mapping_review_{stamp}.csv", index=False, encoding="utf-8-sig")
        message.text = f"<span style='color:#0F766E;font-weight:700;'>Амжилттай:</span> {upload.filename} | {stats['matched_rows']:,}/{stats['inventory_rows']:,} мөр таарсан ({stats['mapping_rate_rows']:.1%}) | Scope: {stats['scope']}"
    except Exception as exc:
        message.text = f"<span style='color:#B91C1C;font-weight:700;'>Алдаа:</span> {friendly_error(exc)}"


def reset_callback() -> None:
    state["inventory"] = current_inventory.copy()
    state["status"] = current_status.copy()
    state["mapping_review"] = current_mapping_review.copy()
    state["scope"] = "NETWORK"
    state["source_file"] = metadata.get("source_files", {}).get("inventory", "Initial snapshot")
    state["mapping_stats"] = metadata.get("mapping", {})
    branch_filter.options = ["ALL"] + sorted(current_status["BranchName"].dropna().astype(str).unique().tolist())
    branch_filter.value = "ALL"
    message.text = "Эхний snapshot сэргээгдлээ."
    refresh_all()


def clear_filters_callback() -> None:
    status_filter.value = []
    ved_filter.value = []
    category_filter.value = []
    confidence_filter.value = []
    branch_filter.value = "ALL"
    search_input.value = ""
    min_gap_slider.value = 0
    critical_only.active = []
    refresh_all()


def _detail_card(label: str, value: str) -> str:
    return (
        '<div style="flex:1 1 0;min-width:110px;background:#F9FAFB;border:1px solid #E5E7EB;'
        'border-radius:8px;padding:8px 10px;">'
        f'<div style="font-size:11px;color:#6B7280;font-weight:600;text-transform:uppercase;">{label}</div>'
        f'<div style="font-size:16px;font-weight:700;color:#111827;margin-top:2px;">{value}</div>'
        '</div>'
    )


def _fmt(v, kind: str = "num") -> str:
    if v is None or pd.isna(v):
        return "—"
    if kind == "num":
        try:
            return f"{float(v):,.2f}"
        except (ValueError, TypeError):
            return str(v)
    return str(v)


def table_selected_callback(attr: str, old, new) -> None:
    if not new:
        return
    idx = new[0]
    d = table_source.data
    sku_ids = d.get("SKU_ID", [])
    if idx >= len(sku_ids):
        return
    sku_id = sku_ids[idx]
    name = d.get("SKU_Name", [""])[idx]
    branch = d.get("BranchName", [""])[idx]
    ved = d.get("VED", [""])[idx]
    cat = d.get("Category", [""])[idx]
    status = d.get("Status", [""])[idx]
    inv = d.get("InventoryPosition", [0])[idx]
    rop = d.get("ROP", [0])[idx]
    gap = d.get("ROPGap", [0])[idx]
    cov = d.get("CoverageRatio", [None])[idx]
    conf = d.get("ROP_Confidence", [""])[idx]
    supplier = d.get("Supplier", [""])[idx]

    master = dim_sku[dim_sku["SKU_ID"] == sku_id] if sku_id is not None and not pd.isna(sku_id) else dim_sku.iloc[0:0]
    lead = master["LeadTime_Months"].iloc[0] if len(master) and "LeadTime_Months" in master else None
    demand = master["AvgMonthlyDemand"].iloc[0] if len(master) and "AvgMonthlyDemand" in master else None
    rop_src = master["ROP_Source"].iloc[0] if len(master) and "ROP_Source" in master else None

    sc = STATUS_COLORS.get(status, "#6B7280")
    cov_str = f"{cov:.1%}" if cov is not None and not pd.isna(cov) else "—"
    html = (
        '<div style="font-family:Segoe UI,Arial,sans-serif;background:white;border:1px solid #E5E7EB;'
        f'border-left:5px solid {sc};border-radius:10px;padding:16px 18px;margin:8px 0;">'
        '<div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px;">'
        '<div>'
        f'<div style="font-size:18px;font-weight:700;color:#111827;">{name}</div>'
        f'<div style="font-size:12.5px;color:#6B7280;margin-top:2px;">SKU ID: <b>{sku_id}</b> · {branch} · VED: <b>{ved}</b> · {cat}</div>'
        '</div>'
        f'<div style="font-size:13px;font-weight:700;color:{sc};background:{sc}18;padding:4px 10px;border-radius:6px;">{status}</div>'
        '</div>'
        '<div style="display:flex;gap:12px;flex-wrap:wrap;margin-top:12px;">'
        f'{_detail_card("Inventory", _fmt(inv))}'
        f'{_detail_card("ROP", _fmt(rop))}'
        f'{_detail_card("Gap", _fmt(gap))}'
        f'{_detail_card("Coverage", cov_str)}'
        f'{_detail_card("Сарын эрэлт", _fmt(demand))}'
        f'{_detail_card("Lead time (сар)", _fmt(lead))}'
        '</div>'
        f'<div style="font-size:12px;color:#6B7280;margin-top:10px;">Нийлүүлэгч: {supplier or "—"} · ROP source: {rop_src or "—"} · Confidence: {conf or "—"}</div>'
        '</div>'
    )
    detail_panel.text = html


def accept_mapping_callback() -> None:
    selected = review_source.selected.indices
    if not selected:
        review_message.text = "<span style='color:#B91C1C;'>Эхлээд хүснэгтээс мөр сонгоно уу.</span>"
        return
    idx = selected[0]
    d = review_source.data
    key = d.get("NormalizedKey", [""])[idx]
    sku_id = d.get("Suggested_SKU_ID", [None])[idx]
    sku_name = d.get("Suggested_SKU_Name", [""])[idx]
    if sku_id is None or pd.isna(sku_id):
        review_message.text = "<span style='color:#B91C1C;'>Энэ мөрөнд санал болгосон SKU байхгүй.</span>"
        return
    global alias_mapping
    new_row = pd.DataFrame([{
        "NormalizedKey": str(key),
        "SKU_ID": int(sku_id),
        "MasterName": str(sku_name),
        "MappingType": "Manual - dashboard review",
        "Notes": "Accepted in dashboard",
    }])
    alias_mapping = pd.concat([alias_mapping, new_row], ignore_index=True)
    try:
        alias_mapping.to_csv(DATA_DIR / "alias_mapping.csv.gz", index=False, encoding="utf-8-sig", compression="gzip")
        alias_mapping.to_csv(BASE_DIR / "powerbi_data" / "alias_mapping.csv", index=False, encoding="utf-8-sig")
    except Exception as exc:
        review_message.text = f"<span style='color:#B91C1C;'>Хадгалахад алдаа: {exc}</span>"
        return
    review_source.patch({"Decision": [(idx, "Зөвшөөрсөн")]})
    review_source.selected.indices = []
    review_message.text = f"<span style='color:#0F766E;font-weight:700;'>Зөвшөөрөв:</span> '{str(key)[:40]}' → SKU {int(sku_id)} alias-д нэмэгдлээ."


def reject_mapping_callback() -> None:
    selected = review_source.selected.indices
    if not selected:
        review_message.text = "<span style='color:#B91C1C;'>Эхлээд хүснэгтээс мөр сонгоно уу.</span>"
        return
    idx = selected[0]
    review_source.patch({"Decision": [(idx, "Татгалзсан")]})
    review_source.selected.indices = []
    review_message.text = "Мөрийг татгалзсан гэж тэмдэглэв."


def threshold_callback(attr: str, old, new) -> None:
    if len(state["inventory"]):
        rebuild_from_inventory(state["inventory"], state["source_file"], state["mapping_review"], state["mapping_stats"])


def filter_callback(attr: str, old, new) -> None:
    refresh_all()


upload.on_change("value", upload_callback)
reset_button.on_click(reset_callback)
clear_filters_button.on_click(clear_filters_callback)
accept_button.on_click(accept_mapping_callback)
reject_button.on_click(reject_mapping_callback)
excess_slider.on_change("value", threshold_callback)
missing_as_zero.on_change("active", threshold_callback)
critical_only.on_change("active", filter_callback)
table_source.selected.on_change("indices", table_selected_callback)
for widget in [status_filter, ved_filter, category_filter, confidence_filter, branch_filter, search_input, min_gap_slider]:
    prop = "value"
    widget.on_change(prop, filter_callback)

# Browser-side CSV export from the currently filtered table source.
download_button = Button(label="Шүүсэн хүснэгт CSV", button_type="success", width=180)
download_button.js_on_click(CustomJS(args=dict(source=table_source), code="""
const data = source.data;
const columns = Object.keys(data).filter(k => k !== 'index');
let csv = columns.join(',') + '\n';
const n = data[columns[0]] ? data[columns[0]].length : 0;
for (let i = 0; i < n; i++) {
  const row = columns.map(c => {
    const value = data[c][i] == null ? '' : String(data[c][i]);
    return '"' + value.replaceAll('"', '""') + '"';
  });
  csv += row.join(',') + '\n';
}
const blob = new Blob(['\ufeff' + csv], {type: 'text/csv;charset=utf-8;'});
const link = document.createElement('a');
link.href = URL.createObjectURL(blob);
link.download = 'rop_filtered.csv';
link.click();
URL.revokeObjectURL(link.href);
"""))

controls = column(
    Div(text="<b>Шинэ үлдэгдэл оруулах</b><br><span style='font-size:11px;color:#6B7280;'>XLSX / XLSB / CSV; сүлжээний болон салбарын формат танина.</span>"),
    upload,
    row(snapshot_picker, excess_slider),
    missing_as_zero,
    row(reset_button, clear_filters_button, download_button),
    critical_only,
    message,
    sizing_mode="stretch_width",
)
filters = row(status_filter, ved_filter, category_filter, confidence_filter, branch_filter, search_input, min_gap_slider, sizing_mode="stretch_width")

executive_panel = TabPanel(
    title="Удирдлагын тойм",
    child=column(
        kpi_container,
        Div(text="<div style='margin:6px 0 2px 0;padding-top:14px;border-top:1px solid #E5E7EB;font-size:15px;font-weight:700;color:#12263A;'>Эрсдэлийн тойм</div>", sizing_mode="stretch_width"),
        gridplot([[status_plot, ved_plot]], sizing_mode="stretch_width"),
        shortage_plot,
        sizing_mode="stretch_width",
    ),
)
analytics_panel = TabPanel(
    title="SKU анализ",
    child=column(scatter_plot, detail_panel, main_table, sizing_mode="stretch_width"),
)
branch_panel = TabPanel(
    title="Салбарын exposure",
    child=column(branch_plot, Div(text="<i>Салбарын үлдэгдэлтэй файл upload хийвэл SKU-салбарын статус мөн шинэчлэгдэнэ.</i>"), sizing_mode="stretch_width"),
)
coverage_panel = TabPanel(
    title="SKU coverage",
    child=column(
        Div(text=(
            "<div style='padding:10px 0;color:#374151;'>"
            "<b>SKU coverage</b> нь тухайн салбар × категори бүлэгт "
            "Inventory Position ≥ ROP байгаа, ROP-той SKU-уудын хувийг харуулна. "
            "Шүүлтүүрүүд болон шинэ inventory upload-д автоматаар шинэчлэгдэнэ."
            "</div>"
        ), sizing_mode="stretch_width"),
        coverage_plot,
        coverage_table,
        sizing_mode="stretch_width",
    ),
)
quality_panel = TabPanel(
    title="Өгөгдлийн чанар",
    child=column(
        quality_plot,
        Div(text="<b>Mapping шалгалт:</b> мөр сонгоод 'Зөвшөөрөх' дарвал alias-д нэмэгдэж, дараагийн upload-д автоматаар таарна.", sizing_mode="stretch_width"),
        row(accept_button, reject_button, review_message),
        review_table,
        sizing_mode="stretch_width",
    ),
)
method_panel = TabPanel(title="Аргачлал", child=methodology)
tabs = Tabs(tabs=[executive_panel, analytics_panel, branch_panel, coverage_panel, quality_panel, method_panel], sizing_mode="stretch_width")

update_branch_chart()
refresh_all()

curdoc().add_root(column(header, freshness_banner, controls, filters, tabs, sizing_mode="stretch_width"))
curdoc().title = "ROP Advanced Dashboard"
