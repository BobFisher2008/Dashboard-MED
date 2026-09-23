from __future__ import annotations

import base64
import json
import time
from dataclasses import replace
from datetime import date, datetime
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
    DateFormatter,
    DatePicker,
    Div,
    FactorRange,
    FileInput,
    GlobalInlineStyleSheet,
    HoverTool,
    HTMLTemplateFormatter,
    InlineStyleSheet,
    Label,
    LinearAxis,
    LinearColorMapper,
    MultiChoice,
    NumberFormatter,
    NumeralTickFormatter,
    RadioButtonGroup,
    Range1d,
    Select,
    Slider,
    Span,
    StringFormatter,
    TableColumn,
    TabPanel,
    Tabs,
    TextInput,
)
from bokeh.palettes import Blues256, RdYlGn11
from bokeh.plotting import figure

from common import (
    STATUS_ORDER,
    VED_CLASSES,
    apply_missing_as_zero,
    build_status_snapshot,
    load_branch_list,
    map_inventory,
    normalize_product_name,
    parse_inventory,
)
from expiry import EXPIRY_STATUS_COLORS, ExpiryConfig, build_batch_expiry, build_dim_expiry_status
from metrics import MetricsCalculator
from queries import CLASS_ORDER, COVERAGE_BANDS, FUTURE_EXPIRY_STATUSES, TOTAL, DashboardQuery, Filters
from warehouse import (
    DATA_DIR,
    RISK_STATUSES,
    branch_rop_wide,
    build_dim_date,
    connect,
    ensure_branches,
    save_snapshot_files,
    to_fact_status,
    upsert_history,
    write_tables,
)

# --------------------------------------------------------------------------
# Data: in-memory DuckDB star schema + the pandas frames the ETL needs
# --------------------------------------------------------------------------
db = DashboardQuery(connect(DATA_DIR))
metadata = json.loads((DATA_DIR / "metadata.json").read_text(encoding="utf-8"))

dim_sku = db.table_frame("dim_sku")
dim_branch = db.table_frame("dim_branch")
fact_branch_rop = db.table_frame("fact_branch_rop")
alias_mapping = db.table_frame("alias_mapping")
initial_status = db.table_frame("fact_sku_status")
initial_inventory = db.table_frame("fact_inventory_snapshot")
initial_mapping_review = db.table_frame("mapping_review")
initial_batches = db.table_frame("fact_batch_expiry") if "fact_batch_expiry" in db.tables() else None
initial_expiry_meta = metadata.get("expiry")

# Active branches: «Салбарын жагсаалт.txt», in file order. Every ROP view,
# filter and chart covers these branches only; without the file, all branches.
BRANCH_LIST_PATH = Path(__file__).resolve().parent / "Салбарын жагсаалт.txt"


def tracked_branches() -> list[tuple[str, str | None]]:
    """(name as listed, the warehouse's BranchName or None) per listed branch.
    Names match exactly or by normalized key (spacing, dashes, case)."""
    if not BRANCH_LIST_PATH.exists():
        return []
    known = {normalize_product_name(b): b for b in dim_branch["BranchName"].dropna().astype(str)}
    return [(name, known.get(normalize_product_name(name))) for name in load_branch_list(BRANCH_LIST_PATH)]


TRACKED = tracked_branches()
TRACKED_NAMES = tuple(dict.fromkeys(data for _, data in TRACKED if data))
db.set_active_branches(TRACKED_NAMES)


def branch_options() -> list[str]:
    """Branch filter choices: the active branches with data, in list order."""
    present = set(db.filter_options()["branches"])
    return [b for b in TRACKED_NAMES if b in present] if TRACKED_NAMES else sorted(present)

STATUS_COLORS = {
    "Тасарсан": "#B91C1C",
    "ROP-оос доош": "#F97316",
    "Өгөгдөл алга": "#7C3AED",
    "ROP байхгүй": "#6B7280",
    "Хэвийн": "#0F766E",
    "Илүүдэл": "#2563EB",
}
VED_COLORS = {"V": "#B91C1C", "E": "#F59E0B", "D": "#0F766E", "Тодорхойгүй": "#6B7280"}
# VED selection: the three classes only - no «Тодорхойгүй» entry.
VED_CHOICES = list(VED_CLASSES)
# Status selection: every status but «Тасарсан» (still counted, shown and in «Зөвхөн эрсдэлтэй»).
# Statuses on the status grids (bar chart, VED / ABC / XYZ x Status). «ROP байхгүй» is
# left off: rows without sales are out of ROP reporting, so it is always empty.
GRID_STATUSES = [s for s in STATUS_ORDER if s != "ROP байхгүй"]
STATUS_CHOICES = [s for s in GRID_STATUSES if s != "Тасарсан"]

state = {
    "inventory": initial_inventory,
    "status_wide": None,  # wide status of the last upload, for the snapshot file
    "mapping_review": initial_mapping_review,
    "source_file": metadata.get("source_files", {}).get("inventory", "Initial snapshot"),
    "mapping_stats": metadata.get("mapping", {}),
}

TITLE_CSS = """
<div style="font-family:Segoe UI,Arial,sans-serif;background:linear-gradient(90deg,#12263A,#1F4E5F);color:white;padding:18px 24px;border-radius:12px;">
  <div style="font-size:26px;font-weight:700;">ROP & Нөөцийн удирдлагын ахисан түвшний дашбоард</div>
  <div style="font-size:13px;opacity:.88;margin-top:4px;">Python ETL + DuckDB star schema + интерактив хяналт</div>
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

options = db.filter_options()
status_filter = MultiChoice(title="Status", value=[], options=STATUS_CHOICES, width=260)
ved_filter = MultiChoice(title="VED", value=[], options=VED_CHOICES, width=220)
category_filter = MultiChoice(title="Категори", value=[], options=options["categories"], width=260)
branch_filter = Select(title="Салбар", value="ALL", options=["ALL"] + branch_options(), width=300)
search_input = TextInput(title="Барааны нэр хайх", placeholder="жишээ: Тобрекс", width=320)
min_gap_slider = Slider(title="Доод ROP gap", start=0, end=max(1000, db.max_gap()), value=0, step=1, width=240)

kpi_container = Div(text="", sizing_mode="stretch_width", height=135)
status_source = ColumnDataSource(data=dict(Status=[], Count=[], Share=[], Label=[], Color=[], Alpha=[]))
top_source = ColumnDataSource(data=dict(
    Label=[], Name=[], Branch=[], VED=[], Status=[], Value=[], ValueText=[], Gap=[], OnHand=[], ROP=[], Color=[],
))
branch_source = ColumnDataSource(data=dict(Branch=[], ROP=[], VROP=[], EROP=[], DROP=[], VShare=[], EShare=[], DShare=[]))
coverage_source = ColumnDataSource(data=dict(
    Branch=[], Category=[], Value=[], AvgCoverage=[], CoveredShare=[], FillRate=[], EligibleRows=[], CoveredRows=[],
    SKUCount=[], ROP=[], Filled=[], Gap=[], Line=[], LineWidth=[],
))
coverage_total_text = ColumnDataSource(data=dict(Branch=[], Category=[], Text=[]))
coverage_bar_source = ColumnDataSource(data=dict(
    Label=[], Value=[], ValueText=[], AvgCoverage=[], CoveredShare=[], FillRate=[], EligibleRows=[], CoveredRows=[],
    SKUCount=[], BranchCount=[], ROP=[], Filled=[], Gap=[],
))
coverage_table_source = ColumnDataSource(data=dict())
quality_source = ColumnDataSource(data=dict(Metric=[], Value=[], Label=[], Color=[]))
table_source = ColumnDataSource(data=dict())
review_source = ColumnDataSource(data=dict())
trend_source = ColumnDataSource(data={"DateLabel": [], "Risk": [], "Critical": [], "Gap": [], "Total": [], **{s: [] for s in STATUS_ORDER}})
movement_source = ColumnDataSource(data=dict())


def empty_figure(title: str, height: int = 360, **ranges):
    """Styled figure; pass categorical x_range / y_range here (not afterwards)
    so Bokeh picks the matching categorical scale."""
    p = figure(title=title, height=height, sizing_mode="stretch_width", toolbar_location="above", **ranges)
    p.grid.grid_line_alpha = 0.15
    p.title.text_font_size = "14pt"
    p.title.text_font_style = "bold"
    return p


DATA_QUALITY_STATUSES = ("Өгөгдөл алга", "ROP байхгүй")
TOP_MODES = ["auto", "gap", "excess", "stock", "missing"]
TOP_TITLES = {
    "gap": ("Нөхөх шаардлагатай TOP 20", "VED-жигнэсэн ROP gap"),
    "excess": ("ROP-оос илүү нөөцтэй TOP 20", "Үлдэгдэл − ROP (ширхэг)"),
    "stock": ("Хамгийн их үлдэгдэлтэй TOP 20", "Inventory Position (ширхэг)"),
    "missing": ("Үлдэгдлийн мэдээлэлгүй — ROP хамгийн өндөр TOP 20", "ROP (ширхэг)"),
}


def no_data_label() -> Label:
    return Label(x=14, y=14, x_units="screen", y_units="screen", visible=False,
                 text="Сонгосон шүүлтүүрт тохирох мэдээлэл алга", text_font_size="12pt", text_color="#6B7280")


# Status mix as labelled bars: a tiny status (e.g. 4 stock-outs next to 120k
# "no data" rows) is still readable from its label. Click a bar to filter.
status_plot = empty_figure("Status бүтэц — SKU × салбарын мөр", 370,
                           y_range=FactorRange(*reversed(GRID_STATUSES)), x_range=Range1d(0, 1))
status_plot.toolbar_location = None
status_bars = status_plot.hbar(
    y="Status", right="Count", height=0.68, source=status_source,
    fill_color="Color", fill_alpha="Alpha", line_color=None,
    nonselection_fill_alpha="Alpha", nonselection_fill_color="Color",
)
status_plot.text(x="Count", y="Status", text="Label", source=status_source, x_offset=6,
                 text_baseline="middle", text_font_size="10pt", text_color="#111827")
status_plot.add_tools(HoverTool(tooltips=[("Status", "@Status"), ("Мөр", "@Count{0,0}"), ("Хувь", "@Share{0.0%}")], renderers=[status_bars]), "tap")
status_plot.xaxis.formatter = NumeralTickFormatter(format="0,0")
status_plot.ygrid.grid_line_color = None
status_plot.yaxis.major_label_text_font_size = "10pt"
status_empty = no_data_label()
status_plot.add_layout(status_empty)

def status_heatmap(label: str, classes: list[str]) -> tuple:
    """<label> x Status heatmap: count and share of the class row in every cell;
    colour = count relative to the largest cell (stays readable with one status
    selected). Returns (figure, source, no-data label)."""
    source = ColumnDataSource(data=dict(Class=[], Status=[], Count=[], Share=[], Intensity=[], Gap=[], Excess=[], Text=[], TextColor=[]))
    mapper = LinearColorMapper(palette=list(reversed(Blues256))[20:], low=0, high=1)
    p = empty_figure(f"{label} × Status — SKU × салбарын мөр", 370,
                     x_range=FactorRange(*GRID_STATUSES), y_range=FactorRange(*reversed(classes)))
    p.toolbar_location = None
    cells = p.rect(
        x="Status", y="Class", width=0.96, height=0.94, source=source, line_color=None,
        fill_color={"field": "Intensity", "transform": mapper},
        nonselection_fill_alpha=1.0,
    )
    p.text(x="Status", y="Class", text="Text", source=source, text_align="center", text_baseline="middle",
           text_font_size="10pt", text_color="TextColor")
    p.add_tools(HoverTool(tooltips=[
        (label, "@Class"), ("Status", "@Status"), ("Мөр", "@Count{0,0}"), (f"{label} доторх хувь", "@Share{0.0%}"),
        ("ROP gap", "@Gap{0,0}"), ("ROP-оос илүү нөөц", "@Excess{0,0}"),
    ], renderers=[cells]), "tap")
    p.grid.grid_line_color = None
    p.axis.axis_line_color = None
    p.axis.major_tick_line_color = None
    p.xaxis.major_label_text_font_size = "9pt"
    p.yaxis.major_label_text_font_size = "11pt"
    empty = no_data_label()
    p.add_layout(empty)
    return p, source, empty


ved_plot, ved_source, ved_empty = status_heatmap("VED", VED_CHOICES)

# TOP 20: ranking follows the status filter (shortage / stock above ROP / stock).
top_mode = RadioButtonGroup(labels=["Автомат", "Дутагдал", "Илүүдэл", "Үлдэгдэл", "Мэдээлэлгүй"], active=0, width=460)
top_level = RadioButtonGroup(labels=["SKU × салбар", "SKU (салбаруудын нийт)"], active=0, width=320)
shortage_plot = figure(
    title="Нөхөх шаардлагатай TOP 20", y_range=FactorRange(), x_range=Range1d(0, 1),
    height=560, sizing_mode="stretch_width", toolbar_location="above",
)
top_bars = shortage_plot.hbar(y="Label", right="Value", height=0.72, color="Color", legend_field="VED", source=top_source)
shortage_plot.text(x="Value", y="Label", text="ValueText", source=top_source, x_offset=5,
                   text_baseline="middle", text_font_size="9pt", text_color="#374151")
shortage_plot.add_tools(HoverTool(tooltips=[
    ("Нэр", "@Name"), ("Салбар", "@Branch"), ("VED", "@VED"), ("Status", "@Status"),
    ("Үлдэгдэл", "@OnHand{0,0.00}"), ("ROP", "@ROP{0,0}"), ("ROP gap", "@Gap{0,0.00}"), ("Утга", "@Value{0,0.00}"),
], renderers=[top_bars]))
shortage_plot.xaxis.axis_label = "VED-жигнэсэн ROP gap"
shortage_plot.xaxis.formatter = NumeralTickFormatter(format="0,0")
shortage_plot.grid.grid_line_alpha = 0.15
shortage_plot.title.text_font_size = "13pt"
shortage_plot.legend.location = "bottom_right"
shortage_plot.legend.title = "VED"
top_empty = no_data_label()
shortage_plot.add_layout(top_empty)

# --- SKU анализ tab -------------------------------------------------------------
# One question per chart: how much stock does each SKU x branch hold against
# its ROP (stock / ROP bands), and are the critical V / E items the short ones?
# Click a band bar to list its rows in the table below.
BANDS = {  # key: (axis label, colour, what it means)
    "OUT": ("Тасарсан", "#991B1B", "Үлдэгдэл 0 — бараа дууссан"),
    "LOW": ("ROP-ийн 50%-иас бага", "#DC2626", "Үлдэгдэл ROP-ийн талаас бага — яаралтай нөхөх"),
    "BELOW": ("ROP-ийн 50–99%", "#F59E0B", "ROP-оос бага — удахгүй нөхөх"),
    "OK": ("ROP-ийн 1–2 дахин", "#16A34A", "Хэвийн нөөц"),
    "HIGH": ("ROP-ийн 2–3 дахин", "#60A5FA", "ROP-оос илүү нөөц"),
    "OVER": ("ROP-ийн 3+ дахин", "#1D4ED8", "Хэт их нөөц — мөнгө хөлдсөн"),
    "NOROP": ("ROP 0", "#6B7280", "Борлуулалттай ч ROP тооцоогүй"),
    "NODATA": ("Үлдэгдлийн мэдээлэлгүй", "#CBD5E1", "Үлдэгдлийн файлд энэ SKU × салбар алга"),
}
SHORT_BANDS = ("OUT", "LOW", "BELOW")

sku_kpis = Div(text="", sizing_mode="stretch_width", height=120)
sku_band = Select(title="Жагсаалтыг шүүх: үлдэгдэл ÷ ROP", value="", width=320,
                  options=[("", "Бүх бүлэг")] + [(k, BANDS[k][0]) for k in COVERAGE_BANDS])

band_source = ColumnDataSource(data=dict(Key=[], Label=[], Rows=[], SKUs=[], Share=[], Text=[], Color=[], Alpha=[],
                                         Desc=[], Gap=[], Excess=[]))
band_plot = empty_figure("Үлдэгдэл ROP-оо хэр хангаж байна вэ? (SKU × салбарын мөр)", 400, x_range=FactorRange())
band_plot.toolbar_location = None
band_bars = band_plot.vbar(
    x="Label", top="Rows", width=0.74, source=band_source, line_color=None,
    fill_color="Color", fill_alpha="Alpha", nonselection_fill_alpha="Alpha", nonselection_fill_color="Color",
)
band_plot.text(x="Label", y="Rows", text="Text", source=band_source, text_align="center", text_baseline="bottom",
               y_offset=-4, text_font_size="10pt", text_font_style="bold", text_color="#111827")
band_plot.add_tools(HoverTool(tooltips=[
    ("", "@Label"), ("Утга", "@Desc"), ("Мөр", "@Rows{0,0} (@Share{0.0%})"), ("SKU", "@SKUs{0,0}"),
    ("ROP хүртэл дутуу", "@Gap{0,0}"), ("ROP-оос илүү", "@Excess{0,0}"),
], renderers=[band_bars]), "tap")
band_plot.y_range = Range1d(0, 1)
band_plot.yaxis.formatter = NumeralTickFormatter(format="0,0")
band_plot.yaxis.axis_label = "SKU × салбарын мөр"
band_plot.xaxis.major_label_text_font_size = "10pt"
band_plot.xaxis.major_label_orientation = 0.35
band_plot.xgrid.grid_line_color = None
band_empty = no_data_label()
band_plot.add_layout(band_empty)

# VED x band as 100% stacked bars: one glance shows whether the critical V
# items are covered as well as the D items.
ved_band_source = ColumnDataSource(data={"VED": [], **{k: [] for k in COVERAGE_BANDS}, **{f"{k}_n": [] for k in COVERAGE_BANDS}})
ved_band_plot = empty_figure("VED ангиллаар — мөрийн бүтэц (%)", 400, y_range=FactorRange(*reversed(VED_CHOICES)),
                             x_range=Range1d(0, 1))
ved_band_plot.toolbar_location = None
ved_band_renderers = ved_band_plot.hbar_stack(
    COVERAGE_BANDS, y="VED", height=0.62, source=ved_band_source,
    color=[BANDS[k][1] for k in COVERAGE_BANDS], line_color="#FFFFFF", line_width=1,
    legend_label=[BANDS[k][0] for k in COVERAGE_BANDS],
)
for _key, _renderer in zip(COVERAGE_BANDS, ved_band_renderers):
    ved_band_plot.add_tools(HoverTool(renderers=[_renderer], tooltips=[
        ("VED", "@VED"), ("Бүлэг", BANDS[_key][0]), ("Мөр", f"@{_key}_n{{0,0}}"), ("Хувь", f"@{_key}{{0.0%}}")]))
ved_band_plot.xaxis.formatter = NumeralTickFormatter(format="0%")
ved_band_plot.yaxis.major_label_text_font_size = "13pt"
ved_band_plot.yaxis.major_label_text_font_style = "bold"
ved_band_plot.ygrid.grid_line_color = None
ved_band_plot.legend.orientation = "horizontal"
ved_band_plot.legend.label_text_font_size = "8.5pt"
ved_band_plot.legend.click_policy = "hide"
ved_band_plot.add_layout(ved_band_plot.legend[0], "below")

branch_plot = figure(title="Салбарын VED ангилал бүтэц — ROP (V / E / D), «Салбарын жагсаалт»-ын дарааллаар", y_range=[],
                     height=610, sizing_mode="stretch_width", toolbar_location="above")
branch_plot.hbar_stack(["VROP", "EROP", "DROP"], y="Branch", height=0.74, color=[VED_COLORS["V"], VED_COLORS["E"], VED_COLORS["D"]], source=branch_source, legend_label=["V", "E", "D"])
branch_plot.add_tools(HoverTool(tooltips=[("Салбар", "@Branch"), ("Нийт ROP", "@ROP{0,0}"), ("V", "@VROP{0,0} (@VShare{0%})"),
                                         ("E", "@EROP{0,0} (@EShare{0%})"), ("D", "@DROP{0,0} (@DShare{0%})")]))
branch_plot.legend.location = "bottom_right"
branch_plot.xaxis.axis_label = "Corrected ROP"

COVERAGE_LEVELS = ["branch_category", "branch", "category", "sku"]
COVERAGE_METRICS = {
    "AvgCoverage": "Дундаж хангалт — min(үлдэгдэл / ROP, 1)-ийн дундаж",
    "CoveredShare": "Хангагдсан мөрийн хувь — үлдэгдэл ≥ ROP",
    "FillRate": "ROP-жигнэсэн хангалт — Σ min(үлдэгдэл, ROP) / Σ ROP",
}
COVERAGE_SHORT = {"AvgCoverage": "Дундаж хангалт", "CoveredShare": "Хангагдсан мөр", "FillRate": "ROP-жигнэсэн"}
coverage_level = RadioButtonGroup(labels=["Салбар × Категори", "Салбар", "Категори", "SKU"], active=0, width=460)
coverage_metric = Select(title="Үзүүлэлт", value="AvgCoverage", options=list(COVERAGE_METRICS.items()), width=440)
coverage_kpis = Div(text="", sizing_mode="stretch_width", height=120)

# red (poor coverage) -> green (fully covered)
coverage_mapper = LinearColorMapper(palette=list(reversed(RdYlGn11)), low=0, high=1, nan_color="#E5E7EB")
coverage_plot = figure(
    title="Хангалт — салбар × категори (НИЙТ мөр / багана = нийлбэр)",
    x_range=FactorRange(), y_range=FactorRange(), height=700, sizing_mode="stretch_width",
    toolbar_location="above",
)
coverage_cells = coverage_plot.rect(
    x="Category", y="Branch", width=1, height=1, source=coverage_source,
    fill_color={"field": "Value", "transform": coverage_mapper},
    line_color="Line", line_width="LineWidth",
)
coverage_plot.text(x="Category", y="Branch", text="Text", source=coverage_total_text, text_align="center",
                   text_baseline="middle", text_font_size="8pt", text_font_style="bold", text_color="#111827")
COVERAGE_TOOLTIPS = [
    ("Дундаж хангалт", "@AvgCoverage{0.0%}"),
    ("Хангагдсан мөр", "@CoveredRows{0,0} / @EligibleRows{0,0} (@CoveredShare{0.0%})"),
    ("ROP-жигнэсэн", "@FillRate{0.0%}"),
    ("SKU", "@SKUCount{0,0}"),
    ("ROP", "@ROP{0,0}"),
    ("Хангагдсан (≤ ROP)", "@Filled{0,0}"),
    ("Дутуу (ROP хүртэл)", "@Gap{0,0}"),
]
coverage_plot.add_tools(HoverTool(tooltips=[("Салбар", "@Branch"), ("Категори", "@Category"), *COVERAGE_TOOLTIPS],
                                  renderers=[coverage_cells]))
coverage_plot.add_layout(ColorBar(
    color_mapper=coverage_mapper, label_standoff=8, title="Хангалт", location=(0, 0),
    formatter=NumeralTickFormatter(format="0%"),
), "right")
coverage_plot.xaxis.major_label_orientation = 0.6
coverage_plot.xaxis.axis_label = "Категори"
coverage_plot.yaxis.axis_label = "Салбар"
coverage_plot.grid.grid_line_color = None
coverage_empty = no_data_label()
coverage_plot.add_layout(coverage_empty)

# Salbar / Kategori / SKU: one bar per group, worst first, dashed line = NIIT.
coverage_bar_plot = figure(
    title="", y_range=FactorRange(), x_range=Range1d(0, 1.15), height=400,
    sizing_mode="stretch_width", toolbar_location="above", visible=False,
)
coverage_bars = coverage_bar_plot.hbar(
    y="Label", right="Value", height=0.72, source=coverage_bar_source, line_color=None,
    fill_color={"field": "Value", "transform": coverage_mapper},
)
coverage_bar_plot.text(x="Value", y="Label", text="ValueText", source=coverage_bar_source, x_offset=5,
                       text_baseline="middle", text_font_size="9pt", text_color="#374151")
coverage_bar_plot.add_tools(HoverTool(tooltips=[
    ("", "@Label"), *COVERAGE_TOOLTIPS, ("Салбарын тоо", "@BranchCount{0,0}"),
], renderers=[coverage_bars]))
coverage_total_span = Span(location=0, dimension="height", line_color="#111827", line_dash="dashed", line_width=1.5)
coverage_total_label = Label(x=0, y=6, y_units="screen", text="", text_font_size="9pt", text_color="#111827", x_offset=4)
coverage_bar_plot.add_layout(coverage_total_span)
coverage_bar_plot.add_layout(coverage_total_label)
coverage_bar_plot.xaxis.formatter = NumeralTickFormatter(format="0%")
coverage_bar_plot.ygrid.grid_line_color = None
coverage_bar_plot.xgrid.grid_line_alpha = 0.15
coverage_bar_empty = no_data_label()
coverage_bar_plot.add_layout(coverage_bar_empty)

_pct = NumberFormatter(format="0.0%")
_int = NumberFormatter(format="0,0")
COVERAGE_MEASURE_COLUMNS = [
    TableColumn(field="EligibleRows", title="ROP-той мөр", formatter=_int, width=95),
    TableColumn(field="CoveredRows", title="Хангагдсан мөр", formatter=_int, width=105),
    TableColumn(field="AvgCoverage", title="Дундаж хангалт", formatter=_pct, width=105),
    TableColumn(field="CoveredShare", title="Хангагдсан %", formatter=_pct, width=95),
    TableColumn(field="FillRate", title="ROP-жигнэсэн", formatter=_pct, width=100),
    TableColumn(field="ROP", title="ROP", formatter=_int, width=90),
    TableColumn(field="Filled", title="Хангагдсан (≤ ROP)", formatter=_int, width=120),
    TableColumn(field="Gap", title="Дутуу", formatter=_int, width=90),
    TableColumn(field="Inventory", title="Нийт үлдэгдэл", formatter=_int, width=105),
]
COVERAGE_GROUP_COLUMNS = {
    "branch_category": [TableColumn(field="BranchName", title="Салбар", width=230),
                        TableColumn(field="Category", title="Категори", width=110),
                        TableColumn(field="SKUCount", title="SKU", formatter=_int, width=70)],
    "branch": [TableColumn(field="BranchName", title="Салбар", width=260),
               TableColumn(field="SKUCount", title="SKU", formatter=_int, width=70)],
    "category": [TableColumn(field="Category", title="Категори", width=160),
                 TableColumn(field="SKUCount", title="SKU", formatter=_int, width=70),
                 TableColumn(field="BranchCount", title="Салбар", formatter=_int, width=70)],
    "sku": [TableColumn(field="SKU_ID", title="SKU ID", width=70),
            TableColumn(field="SKU_Name", title="Нэр төрөл", width=320),
            TableColumn(field="Category", title="Категори", width=100),
            TableColumn(field="BranchCount", title="Салбар", formatter=_int, width=70)],
}
coverage_table = DataTable(
    source=coverage_table_source, columns=COVERAGE_GROUP_COLUMNS["branch_category"] + COVERAGE_MEASURE_COLUMNS,
    height=520, sizing_mode="stretch_width", index_position=None,
)
for _c in [c for cols in COVERAGE_GROUP_COLUMNS.values() for c in cols] + COVERAGE_MEASURE_COLUMNS:
    _c.sortable = True

# --- ABC / XYZ tab ------------------------------------------------------------
# The class selectors filter this tab only; the global filters above apply too.
abc_filter = MultiChoice(title="ABC ангилал", value=[], options=CLASS_ORDER["ABC"], width=280)
xyz_filter = MultiChoice(title="XYZ ангилал", value=[], options=CLASS_ORDER["XYZ"], width=280)
class_order = RadioButtonGroup(labels=["ABC-ээр", "XYZ-ээр"], active=0, width=200)
abc_plot, abc_source, abc_empty = status_heatmap("ABC", CLASS_ORDER["ABC"])
xyz_plot, xyz_source, xyz_empty = status_heatmap("XYZ", CLASS_ORDER["XYZ"])
CLASS_SUMMARY_FIELDS = ["Class", "Rows", "SalesShare", "ROP", "Inventory", "Gap", "RiskRows", "CoveredShare"]
_pct_or_dash = NumberFormatter(format="0.0%", nan_format="—")


def class_summary_table(label: str) -> tuple[DataTable, ColumnDataSource]:
    source = ColumnDataSource(data={f: [] for f in CLASS_SUMMARY_FIELDS})
    columns = [
        TableColumn(field="Class", title=label, width=95),
        TableColumn(field="Rows", title="SKU × салбар", formatter=_int, width=90),
        TableColumn(field="SalesShare", title="Борлуулалтын хувь", formatter=_pct_or_dash, width=115),
        TableColumn(field="ROP", title="ROP", formatter=_int, width=75),
        TableColumn(field="Inventory", title="Үлдэгдэл", formatter=_int, width=85),
        TableColumn(field="Gap", title="ROP gap", formatter=_int, width=75),
        TableColumn(field="RiskRows", title="Эрсдэлтэй мөр", formatter=_int, width=95),
        TableColumn(field="CoveredShare", title="Хангагдсан %", formatter=_pct_or_dash, width=90),
    ]
    table = DataTable(source=source, columns=columns, height=185, sizing_mode="stretch_width",
                      index_position=None, sortable=False)
    return table, source


abc_summary, abc_summary_source = class_summary_table("ABC")
xyz_summary, xyz_summary_source = class_summary_table("XYZ")
class_items_source = ColumnDataSource(data=dict())
class_item_columns = [
    TableColumn(field="SKU_ID", title="SKU ID", formatter=NumberFormatter(format="0"), width=70),
    TableColumn(field="SKU_Name", title="Нэр төрөл", width=300),
    TableColumn(field="BranchName", title="Салбар", width=220),
    TableColumn(field="ABC", title="ABC", width=55),
    TableColumn(field="XYZ", title="XYZ", width=55),
    TableColumn(field="ClassSource", title="Ангиллын эх", width=100),
    TableColumn(field="VED", title="VED", width=50),
    TableColumn(field="Category", title="Категори", width=90),
    TableColumn(field="Status", title="Status", width=115),
    TableColumn(field="InventoryPosition", title="Inventory", formatter=NumberFormatter(format="0,0.00"), width=95),
    TableColumn(field="ROP", title="ROP", formatter=_int, width=75),
    TableColumn(field="ROPGap", title="Gap", formatter=NumberFormatter(format="0,0.00"), width=85),
    TableColumn(field="CoverageRatio", title="Coverage", formatter=_pct, width=85),
    TableColumn(field="Sales7M", title="7 сарын дун. борлуулалт", formatter=_int, width=150),
]
class_items_table = DataTable(source=class_items_source, columns=class_item_columns, height=520,
                              sizing_mode="stretch_width", index_position=None)
for _c in class_item_columns:
    _c.sortable = True

# --- Хугацаат tab ---------------------------------------------------------------
# Batches with a valid expiry date still ahead, tracked per branch of
# «Салбарын жагсаалт.txt» (file order). The global VED / category / search
# filters apply; the ROP filters (status, gap, branch) do not.
EXPIRY_LIST, EXPIRY_ALL = "__LIST__", "__ALL__"
expiry_labels = dict(zip(*build_dim_expiry_status(ExpiryConfig.load())[["ExpiryStatus", "StatusLabel"]].T.values))
EXPIRY_LABELS = {s: expiry_labels[s] for s in FUTURE_EXPIRY_STATUSES}
def expiry_branch_options() -> list[tuple[str, str]]:
    return ([(EXPIRY_LIST, f"Идэвхтэй салбарууд ({len(TRACKED)})"), (EXPIRY_ALL, "Бүх салбар (жагсаалтад байхгүй ч)")]
            + [(data or listed, listed if data else f"{listed} — өгөгдөлгүй") for listed, data in TRACKED])


expiry_branch = Select(title="Салбар (Салбарын жагсаалт)", value=EXPIRY_LIST, width=380, options=expiry_branch_options())
expiry_status = MultiChoice(title="Хугацааны бүлэг", value=[], width=380,
                            options=[(s, EXPIRY_LABELS[s]) for s in FUTURE_EXPIRY_STATUSES])
expiry_kpis = Div(text="", sizing_mode="stretch_width", height=120)
expiry_note = Div(text="", sizing_mode="stretch_width")

expiry_month_source = ColumnDataSource(data={"Month": [], "Label": [], **{s: [] for s in FUTURE_EXPIRY_STATUSES}})
expiry_month_plot = empty_figure("Дуусах сараар — үлдэгдлийн тоо (ширхэг)", 380, x_range=FactorRange())
expiry_month_plot.vbar_stack(
    FUTURE_EXPIRY_STATUSES, x="Month", width=0.8, source=expiry_month_source,
    color=[EXPIRY_STATUS_COLORS[s] for s in FUTURE_EXPIRY_STATUSES],
    legend_label=[EXPIRY_LABELS[s] for s in FUTURE_EXPIRY_STATUSES],
)
expiry_month_plot.add_tools(HoverTool(tooltips=[("Сар", "@Label"), ("Бүлэг", "$name"), ("Тоо", "@$name{0,0}")]))
expiry_month_plot.y_range.start = 0
expiry_month_plot.yaxis.formatter = NumeralTickFormatter(format="0,0")
expiry_month_plot.xaxis.major_label_orientation = 0.9
expiry_month_plot.xgrid.grid_line_color = None
expiry_month_plot.legend.location = "top_right"
expiry_month_plot.legend.label_text_font_size = "9pt"
expiry_month_plot.legend.background_fill_alpha = 0.8
expiry_month_empty = no_data_label()
expiry_month_plot.add_layout(expiry_month_empty)

# Branch tracking: one row per listed branch, in the list's order; the chart
# ranks the branches with the most stock expiring within the critical window.
EXPIRY_BRANCH_FIELDS = ["No", "BranchName", "BranchGroup", "Batches", "SKUs", "Qty", *FUTURE_EXPIRY_STATUSES, "Nearest", "AtRisk", "Tracking"]
expiry_branch_source = ColumnDataSource(data={f: [] for f in EXPIRY_BRANCH_FIELDS})
expiry_branch_columns = [
    TableColumn(field="No", title="№", width=40),
    TableColumn(field="BranchName", title="Салбар", width=260),
    TableColumn(field="BranchGroup", title="Бүлэг", width=85),
    TableColumn(field="Batches", title="Багц", formatter=_int, width=70),
    TableColumn(field="SKUs", title="SKU", formatter=_int, width=65),
    TableColumn(field="Qty", title="Нийт тоо", formatter=_int, width=85),
    *[TableColumn(field=s, title=EXPIRY_LABELS[s], formatter=_int, width=90) for s in FUTURE_EXPIRY_STATUSES],
    TableColumn(field="Nearest", title="Хамгийн ойр дуусах", formatter=DateFormatter(format="%Y-%m-%d"), width=125),
    TableColumn(field="AtRisk", title="Эрсдэлтэй багц", formatter=_int, width=105),
    TableColumn(field="Tracking", title="Хяналт", width=110),
]
expiry_branch_table = DataTable(source=expiry_branch_source, columns=expiry_branch_columns, height=420,
                                sizing_mode="stretch_width", index_position=None, selectable=True)
expiry_branch_plot_source = ColumnDataSource(data={"Branch": [], **{s: [] for s in FUTURE_EXPIRY_STATUSES}})
expiry_branch_plot = empty_figure("Салбарууд — дуусах хугацааны бүлгээр (эхний 25, яаралтай нь дээрээ)", 560, y_range=FactorRange())
expiry_branch_plot.hbar_stack(
    FUTURE_EXPIRY_STATUSES, y="Branch", height=0.72, source=expiry_branch_plot_source,
    color=[EXPIRY_STATUS_COLORS[s] for s in FUTURE_EXPIRY_STATUSES],
    legend_label=[EXPIRY_LABELS[s] for s in FUTURE_EXPIRY_STATUSES],
)
expiry_branch_plot.add_tools(HoverTool(tooltips=[("Салбар", "@Branch"), ("Бүлэг", "$name"), ("Тоо", "@$name{0,0}")]))
expiry_branch_plot.x_range.start = 0
expiry_branch_plot.xaxis.formatter = NumeralTickFormatter(format="0,0")
expiry_branch_plot.ygrid.grid_line_color = None
expiry_branch_plot.legend.location = "bottom_right"
expiry_branch_plot.legend.label_text_font_size = "9pt"
expiry_branch_empty = no_data_label()
expiry_branch_plot.add_layout(expiry_branch_empty)

expiry_items_source = ColumnDataSource(data=dict())
expiry_item_columns = [
    TableColumn(field="SKU_ID", title="SKU ID", formatter=NumberFormatter(format="0"), width=70),
    TableColumn(field="SKU_Name", title="Нэр төрөл", width=300),
    TableColumn(field="BranchName", title="Салбар", width=230),
    TableColumn(field="Category", title="Категори", width=90),
    TableColumn(field="VED", title="VED", width=50),
    TableColumn(field="ExpiryDate", title="Дуусах огноо", formatter=DateFormatter(format="%Y-%m-%d"), width=100),
    TableColumn(field="DTE", title="Үлдсэн хоног", formatter=_int, width=95),
    TableColumn(field="StatusLabel", title="Бүлэг", width=100),
    TableColumn(field="Qty", title="Тоо", formatter=NumberFormatter(format="0,0.##"), width=75),
    TableColumn(field="FEFORank", title="FEFO", formatter=_int, width=55),
    TableColumn(field="ProjectedWaste", title="Төсөөлсөн хаягдал", formatter=NumberFormatter(format="0,0.##", nan_format="—"), width=120),
    TableColumn(field="Risk", title="Эрсдэлтэй", width=80),
]
expiry_items_table = DataTable(source=expiry_items_source, columns=expiry_item_columns, height=520,
                               sizing_mode="stretch_width", index_position=None)
for _c in expiry_branch_columns + expiry_item_columns:
    _c.sortable = True

quality_plot = figure(title="Өгөгдлийн чанарын KPI", x_range=[], height=410, sizing_mode="stretch_width", toolbar_location=None)
quality_plot.vbar(x="Metric", top="Value", width=0.68, color="Color", source=quality_source)
quality_plot.text(x="Metric", y="Value", text="Label", source=quality_source, text_align="center", text_baseline="bottom", text_font_size="10pt")
quality_plot.xaxis.major_label_orientation = 0.8
quality_plot.y_range.start = 0
quality_plot.grid.grid_line_alpha = 0.15

STATUS_PILL = HTMLTemplateFormatter(template=(
    '<span style="display:inline-block;padding:1px 9px;border-radius:10px;font-size:11.5px;font-weight:600;'
    'color:<%= StatusColor %>;background:<%= StatusColor %>1F;border:1px solid <%= StatusColor %>55;"><%= value %></span>'))
RATIO_BAR = HTMLTemplateFormatter(template=(
    '<div style="display:flex;align-items:center;gap:6px;height:100%;">'
    '<div style="position:relative;flex:1;height:8px;background:#E5E7EB;border-radius:4px;overflow:hidden;">'
    '<div style="width:<%= BarPct %>%;height:100%;background:<%= BandColor %>;"></div>'
    '<div style="position:absolute;left:33.3%;top:0;bottom:0;width:1px;background:#111827;opacity:.45;"></div></div>'
    '<span style="min-width:42px;text-align:right;font-variant-numeric:tabular-nums;"><%= RatioText %></span></div>'))
main_columns = [
    TableColumn(field="SKU_ID", title="SKU ID", formatter=NumberFormatter(format="0"), width=65),
    TableColumn(field="SKU_Name", title="Нэр төрөл", formatter=StringFormatter(), width=320),
    TableColumn(field="BranchName", title="Салбар", width=220),
    TableColumn(field="VED", title="VED", width=45),
    TableColumn(field="Category", title="Категори", width=85),
    TableColumn(field="Status", title="Status", formatter=STATUS_PILL, width=125),
    TableColumn(field="InventoryPosition", title="Үлдэгдэл", formatter=NumberFormatter(format="0,0.[00]"), width=80),
    TableColumn(field="ROP", title="ROP", formatter=NumberFormatter(format="0,0"), width=65),
    TableColumn(field="RatioText", title="Үлдэгдэл ÷ ROP (зураас = ROP)", formatter=RATIO_BAR, width=190),
    TableColumn(field="ROPGap", title="Дутуу", formatter=NumberFormatter(format="0,0.[00]"), width=75),
    TableColumn(field="ROP_Confidence", title="Confidence", width=80),
    TableColumn(field="Supplier", title="Нийлүүлэгч", width=200),
]
table_title = Div(text="", sizing_mode="stretch_width")
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

# --- Trend tab --------------------------------------------------------------
trend_include_network = CheckboxGroup(labels=["Сүлжээний (NETWORK) snapshot-ийг оруулах"], active=[], width=320)
movement_from = Select(title="Харьцуулах: эхний огноо", value="", options=[], width=220)
movement_to = Select(title="Сүүлийн огноо", value="", options=[], width=220)
trend_summary = Div(text="", sizing_mode="stretch_width", height=120)
movement_summary_div = Div(text="", sizing_mode="stretch_width")

trend_mix_plot = figure(
    title="Status бүтэц — snapshot бүрээр (%)", x_range=FactorRange(), height=400,
    sizing_mode="stretch_width", toolbar_location="above",
)
trend_mix_renderers = trend_mix_plot.vbar_stack(
    STATUS_ORDER, x="DateLabel", width=0.7, source=trend_source,
    color=[STATUS_COLORS[s] for s in STATUS_ORDER], legend_label=STATUS_ORDER,
)
trend_mix_plot.add_tools(HoverTool(tooltips=[("Огноо", "@DateLabel"), ("Status", "$name"), ("Хувь", "@$name{0.0%}")]))
trend_mix_plot.y_range.start = 0
trend_mix_plot.y_range.end = 1
trend_mix_plot.yaxis.formatter = NumeralTickFormatter(format="0%")
trend_mix_plot.legend.location = "top_left"
trend_mix_plot.legend.orientation = "horizontal"
trend_mix_plot.legend.click_policy = "hide"
trend_mix_plot.legend.label_text_font_size = "9pt"
trend_mix_plot.add_layout(trend_mix_plot.legend[0], "below")
trend_mix_plot.xgrid.grid_line_color = None

trend_risk_plot = figure(
    title="Эрсдэлтэй SKU, critical V/E ба ROP gap", x_range=trend_mix_plot.x_range, height=380,
    sizing_mode="stretch_width", toolbar_location="above",
)
risk_line = trend_risk_plot.line(x="DateLabel", y="Risk", source=trend_source, color=STATUS_COLORS["ROP-оос доош"], line_width=2.5, legend_label="Эрсдэлтэй (Тасарсан + ROP-оос доош)")
trend_risk_plot.scatter(x="DateLabel", y="Risk", source=trend_source, color=STATUS_COLORS["ROP-оос доош"], size=8)
critical_line = trend_risk_plot.line(x="DateLabel", y="Critical", source=trend_source, color=VED_COLORS["V"], line_width=2.5, legend_label="Critical V/E")
trend_risk_plot.scatter(x="DateLabel", y="Critical", source=trend_source, color=VED_COLORS["V"], size=8)
trend_risk_plot.extra_y_ranges = {"gap": DataRange1d(start=0)}
trend_risk_plot.add_layout(LinearAxis(y_range_name="gap", axis_label="ROP gap", formatter=NumeralTickFormatter(format="0,0")), "right")
gap_line = trend_risk_plot.line(x="DateLabel", y="Gap", source=trend_source, y_range_name="gap", color="#111827", line_dash="dashed", line_width=2, legend_label="ROP gap")
trend_risk_plot.y_range = DataRange1d(start=0, renderers=[risk_line, critical_line])
trend_risk_plot.extra_y_ranges["gap"].renderers = [gap_line]
trend_risk_plot.add_tools(HoverTool(tooltips=[
    ("Огноо", "@DateLabel"), ("Эрсдэлтэй", "@Risk{0,0}"), ("Critical V/E", "@Critical{0,0}"),
    ("ROP gap", "@Gap{0,0}"), ("Нийт мөр", "@Total{0,0}"),
], renderers=[risk_line], mode="vline"))
trend_risk_plot.yaxis[0].axis_label = "SKU × салбар"
trend_risk_plot.yaxis[0].formatter = NumeralTickFormatter(format="0,0")
trend_risk_plot.legend.location = "top_right"
trend_risk_plot.legend.label_text_font_size = "9pt"
trend_risk_plot.legend.background_fill_alpha = 0.7
trend_risk_plot.xgrid.grid_line_color = None

movement_columns = [
    TableColumn(field="Direction", title="Чиглэл", width=95),
    TableColumn(field="SKU_ID", title="SKU ID", formatter=NumberFormatter(format="0"), width=70),
    TableColumn(field="SKU_Name", title="Нэр төрөл", width=300),
    TableColumn(field="BranchName", title="Салбар", width=220),
    TableColumn(field="VED", title="VED", width=50),
    TableColumn(field="Category", title="Категори", width=90),
    TableColumn(field="StatusFrom", title="Өмнө", width=110),
    TableColumn(field="StatusTo", title="Одоо", width=110),
    TableColumn(field="InventoryFrom", title="Inventory өмнө", formatter=NumberFormatter(format="0,0.00"), width=105),
    TableColumn(field="InventoryTo", title="Inventory одоо", formatter=NumberFormatter(format="0,0.00"), width=105),
    TableColumn(field="ROP", title="ROP", formatter=NumberFormatter(format="0,0"), width=70),
    TableColumn(field="ROPGap", title="Gap", formatter=NumberFormatter(format="0,0.00"), width=80),
]
movement_table = DataTable(source=movement_source, columns=movement_columns, height=480, sizing_mode="stretch_width", index_position=None)

for _c in main_table.columns + review_table.columns + movement_table.columns:
    _c.sortable = True

methodology = Div(text="""
<div style="font-family:Segoe UI,Arial,sans-serif;line-height:1.55;padding:16px 20px;background:white;border-radius:10px;border:1px solid #E5E7EB;">
<h2 style="color:#12263A;">ROP аргачлал</h2>
<p><b>Corrected ROP</b> = CEILING(AvgDemand × MeanLT + Z<sub>final</sub> × √(MeanLT × DemandSD² + AvgDemand² × LTSD²), 1)</p>
<ul>
<li>Z<sub>final</sub> = MAX(Z VED, Z ABC–XYZ)</li>
<li>Inventory Position = On Hand + On Order − Backorder</li>
<li>Status: тасарсан, ROP-оос доош, хэвийн, илүүдэл, өгөгдөл алга, ROP байхгүй</li>
<li><b>Дундаж борлуулалтгүй мөр</b> (ROP файлд «Сарын дундаж тоо» хоосон эсвэл 0, эсвэл ROP файлд огт байхгүй SKU × салбар):
ROP тооцохгүй, ROP-ийн тайланд огт оруулахгүй — «ROP байхгүй», «Өгөгдөл алга»-д ч тоологдохгүй. Хугацаат табад харагдана.</li>
<li>Дэлгэрэнгүй ROP байхгүй SKU-д fallback ROP хадгалсан бөгөөд confidence = “Бага”.</li>
<li><b>Идэвхтэй салбарууд</b>: бүх таб, KPI, тренд зөвхөн «Салбарын жагсаалт.txt»-д буй салбаруудыг хамарна (файлыг засаад дашбоардыг дахин ачаална).</li>
</ul>
<h3 style="color:#12263A;">Өгөгдлийн загвар (star schema)</h3>
<p>Өгөгдөл <code>data/*.parquet</code> файлд хадгалагдаж, DuckDB-ээр SQL-ээр шүүгдэнэ.
Fact хүснэгтүүд (<code>fact_sku_status</code>, <code>fact_status_history</code>, <code>fact_branch_rop</code>) зөвхөн түлхүүр ба хэмжигдэхүүн агуулна;
барааны нэр, категори, салбарын нэр зэрэг тайлбар мэдээлэл <code>dim_sku</code>, <code>dim_branch</code>, <code>dim_date</code> хүснэгтээс холбогдоно.
Категори нь <code>SKU category.csv</code> файлаас авна; тохирохгүй SKU-г <code>data/category_review.csv</code>-д жагсаана.
VED нь <code>VED ангилал.csv</code> файлаас авна: файлд байгаа SKU тэр VED-ийг бүх салбар, бүх snapshot-д авч, файлд байхгүй SKU ROP файлын VED-ээ хадгална.
ROP ба Z VED нь ROP файлд тооцоолсон хэвээр (тооцоонд ашигласан VED <code>VED_ROP</code> баганад); ROP файлын VED-ээс өөр болсон SKU-г <code>data/ved_review.csv</code>-д жагсаана.
Snapshot бүр <code>fact_status_history</code>-д огноогоор (DateKey) хадгалагдаж, «Тренд» табад харагдана.</p>
<h3 style="color:#12263A;">ABC / XYZ</h3>
<p>ROP файлын салбар бүрийн ангилал (ROP файлд ангилалгүй мөрийг «ABC XYZ.xlsx» файлаас нөхнө; «Ангиллын эх» баганад): <b>ABC</b> — борлуулалтын дүнгээр (A — борлуулалтын дийлэнх), <b>XYZ</b> — эрэлтийн хэлбэлзлээр (X — тогтвортой, Z — хамгийн тогтворгүй).
«ABC / XYZ» таб хоёр ангиллыг тус тусад нь status-аар задалж, SKU × салбарын жагсаалтыг ангиллаар эрэмбэлнэ.</p>
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


def current_filters() -> Filters:
    return Filters(
        statuses=tuple(status_filter.value),
        veds=tuple(ved_filter.value),
        categories=tuple(category_filter.value),
        branch=None if branch_filter.value == "ALL" else branch_filter.value,
        critical_only=bool(critical_only.active),
        search=search_input.value or "",
        min_gap=float(min_gap_slider.value or 0),
    )


def update_kpis(f: Filters) -> None:
    k = db.kpis(f)
    mapping_rate = state.get("mapping_stats", {}).get("mapping_rate_rows", 0)
    html = '<div style="display:flex;gap:12px;flex-wrap:wrap;width:100%;box-sizing:border-box;font-family:Segoe UI,Arial,sans-serif;">'
    html += card("Шүүсэн SKU", f"{k['sku_count']:,.0f}", f"Scope: {db.scope()}", "#1F4E5F")
    html += card("Inventory Position", f"{k['inventory_position']:,.0f}", "Matched inventory", "#2563EB")
    html += card("Total ROP", f"{k['total_rop']:,.0f}", "Ашиглах ROP", "#0F766E")
    html += card("ROP gap", f"{k['rop_gap']:,.0f}", "Нөхөх шаардлагатай", "#F97316")
    html += card("Critical V/E", f"{k['critical']:,.0f}", "Тасарсан эсвэл ROP-оос доош", "#B91C1C")
    html += card("ROP-оос илүү нөөц", f"{k['excess']:,.0f}", "Σ (үлдэгдэл − ROP), ROP-той мөр", "#2563EB")
    html += card("Mapping rate", f"{mapping_rate:.1%}", state.get("source_file", ""), "#7C3AED")
    html += "</div>"
    kpi_container.text = html


def update_freshness() -> None:
    snapshot = str(snapshot_picker.value or metadata.get("snapshot_date", ""))
    rate = state.get("mapping_stats", {}).get("mapping_rate_rows", 0)
    freshness_banner.text = (
        '<div style="display:flex;gap:18px;align-items:center;flex-wrap:wrap;'
        'font-family:Segoe UI,Arial,sans-serif;font-size:12.5px;color:#374151;'
        'padding:8px 14px;background:#F3F4F6;border-radius:8px;margin-top:8px;">'
        f'<span><b style="color:#12263A;">Сүүлийн шинэчлэл:</b> {snapshot}</span>'
        f'<span><b style="color:#12263A;">Эх файл:</b> {state.get("source_file", "")}</span>'
        f'<span><b style="color:#12263A;">Mapping:</b> {rate:.1%}</span>'
        f'<span><b style="color:#12263A;">Scope:</b> {db.scope()}</span>'
        f'<span style="margin-left:auto;color:#0F766E;">{state.get("live_text", "")}</span>'
        '</div>'
    )


def _share_text(share: float) -> str:
    return "<0.1%" if 0 < share < 0.001 else f"{share:.1%}"


def update_status_chart(f: Filters) -> None:
    counts = db.status_counts(f).reindex(GRID_STATUSES)
    total = int(counts.sum())
    shares = counts / total if total else counts * 0.0
    status_source.data = {
        "Status": list(counts.index),
        "Count": counts.astype(int).tolist(),
        "Share": shares.tolist(),
        "Label": [f"{c:,}  ·  {_share_text(s)}" for c, s in zip(counts, shares)],
        "Color": [STATUS_COLORS[s] for s in counts.index],
        # data-quality statuses are muted so the operational ones stand out
        "Alpha": [0.35 if s in DATA_QUALITY_STATUSES else 0.95 for s in counts.index],
    }
    status_plot.x_range.end = max(total and counts.max() * 1.3, 1)
    status_empty.visible = total == 0


def fill_status_heatmap(plot, source: ColumnDataSource, empty: Label, cells: pd.DataFrame, classes: list[str]) -> None:
    """cells: Class, Status, Count, Share, Intensity, Gap, Excess (MetricsCalculator.heatmap_cells)."""
    cells = cells[cells["Status"].isin(GRID_STATUSES)]
    source.data = {
        "Class": cells["Class"].tolist(),
        "Status": cells["Status"].tolist(),
        "Count": cells["Count"].astype(int).tolist(),
        "Share": cells["Share"].tolist(),
        "Intensity": cells["Intensity"].tolist(),
        "Gap": cells["Gap"].astype(float).tolist(),
        "Excess": cells["Excess"].astype(float).tolist(),
        "Text": [f"{c:,}\n{s:.0%}" for c, s in zip(cells["Count"], cells["Share"])],
        "TextColor": ["#FFFFFF" if i > 0.55 else "#111827" for i in cells["Intensity"]],
    }
    plot.x_range.factors = [s for s in GRID_STATUSES if s in set(cells["Status"])] or GRID_STATUSES
    plot.y_range.factors = list(reversed([c for c in classes if c in set(cells["Class"])] or classes))
    empty.visible = cells.empty


def update_ved_chart(f: Filters) -> None:
    cells = MetricsCalculator.heatmap_cells(db.ved_status(f).rename(columns={"VED": "Class"}))
    cells = cells[cells["Class"].isin(VED_CHOICES)]
    fill_status_heatmap(ved_plot, ved_source, ved_empty, cells, VED_CHOICES)


def class_filters(f: Filters) -> Filters:
    """The global filters plus this tab's ABC / XYZ selection."""
    return replace(f, abc=tuple(abc_filter.value), xyz=tuple(xyz_filter.value))


def update_class_tab(f: Filters) -> None:
    cf = class_filters(f)
    breakdown = db.class_breakdown(cf)
    for column, (plot, source, empty), summary in [
        ("ABC", (abc_plot, abc_source, abc_empty), abc_summary_source),
        ("XYZ", (xyz_plot, xyz_source, xyz_empty), xyz_summary_source),
    ]:
        cells, classes = MetricsCalculator.class_rollup(breakdown, column, CLASS_ORDER[column])
        fill_status_heatmap(plot, source, empty, cells, CLASS_ORDER[column])
        summary.data = ColumnDataSource.from_df(classes[CLASS_SUMMARY_FIELDS])
    order = "XYZ" if class_order.active == 1 else "ABC"
    class_items_source.data = ColumnDataSource.from_df(db.class_items(cf, order))


def expiry_scope() -> tuple[str, ...]:
    """BranchNames the Хугацаат tab covers: the tracked list, every branch (),
    or the one selected branch."""
    if expiry_branch.value == EXPIRY_LIST:
        return TRACKED_NAMES or tuple(listed for listed, _ in TRACKED)
    if expiry_branch.value == EXPIRY_ALL:
        return ()
    return (expiry_branch.value,)


def _date_text(value) -> str:
    return "—" if value is None or pd.isna(value) else pd.Timestamp(value).strftime("%Y-%m-%d")


def update_expiry(f: Filters) -> None:
    branches, statuses = expiry_scope(), tuple(expiry_status.value)
    k = db.expiry_kpis(f, branches, statuses)
    html = '<div style="display:flex;gap:12px;flex-wrap:wrap;width:100%;box-sizing:border-box;font-family:Segoe UI,Arial,sans-serif;">'
    html += card("Хугацаатай багц", f"{k['batches']:,.0f}", f"{k['skus']:,.0f} SKU · {k['branches']:,.0f} салбар", "#1F4E5F")
    html += card("Нийт үлдэгдэл", f"{k['qty']:,.0f}", "Хугацаа нь дуусаагүй, ширхэг", "#2563EB")
    html += card(EXPIRY_LABELS["CRITICAL"], f"{k['critical_qty']:,.0f}", f"{k['critical_batches']:,.0f} багц — яаралтай", EXPIRY_STATUS_COLORS["CRITICAL"])
    html += card(EXPIRY_LABELS["WARNING"], f"{k['warning_qty']:,.0f}", "ширхэг", EXPIRY_STATUS_COLORS["WARNING"])
    html += card("Эрсдэлтэй багц", f"{k['at_risk_batches']:,.0f}", f"Төсөөлсөн хаягдал {k['projected_waste']:,.0f} ширхэг", "#7F1D1D")
    html += card("Хамгийн ойр дуусах", _date_text(k["nearest"]), "огноо", "#6B7280")
    expiry_kpis.text = html + "</div>"

    # Monthly profile: the next 24 months. Later dates would dwarf them as one
    # bar, so their total goes in the title instead.
    months = db.expiry_months(f, branches, statuses)
    later = float(months.loc[months["Month"].isna(), "Qty"].sum())
    months = months[months["Month"].notna()]
    expiry_month_plot.title.text = f"Дуусах сараар — ирэх 24 сар, үлдэгдлийн тоо (ширхэг) · 24+ сарын дараа: {later:,.0f}"
    labels = months["Month"].map(lambda m: pd.Timestamp(m).strftime("%Y-%m"))
    wide = months.assign(Label=labels).pivot_table(index="Label", columns="ExpiryStatus", values="Qty", aggfunc="sum", sort=False)
    wide = wide.reindex(columns=FUTURE_EXPIRY_STATUSES).fillna(0)
    expiry_month_source.data = {"Month": list(wide.index), "Label": list(wide.index),
                                **{s: wide[s].tolist() for s in FUTURE_EXPIRY_STATUSES}}
    expiry_month_plot.x_range.factors = list(wide.index)
    expiry_month_empty.visible = wide.empty

    # Branch tracking table: the tracked list in file order (branches without
    # batches included), or every branch with batches, most urgent first.
    by_branch = db.expiry_by_branch(f, branches, statuses)
    if expiry_branch.value == EXPIRY_LIST:
        listed = pd.DataFrame([(i + 1, data or name, data is not None) for i, (name, data) in enumerate(TRACKED)],
                              columns=["No", "BranchName", "InWarehouse"])
        table = listed.merge(by_branch, on="BranchName", how="left")
        table["Tracking"] = np.select(
            [~table["InWarehouse"], table["Batches"].isna()], ["Өгөгдөлгүй салбар", "Хугацаатай бараагүй"], "Хянагдаж буй")
    else:
        table = by_branch.sort_values(["CRITICAL", "WARNING"], ascending=False).reset_index(drop=True)
        table.insert(0, "No", range(1, len(table) + 1))
        tracked = set(TRACKED_NAMES)
        table["Tracking"] = np.where(table["BranchName"].isin(tracked), "Жагсаалтад", "Жагсаалтад байхгүй")
    for col in ["Batches", "SKUs", "Qty", "AtRisk", *FUTURE_EXPIRY_STATUSES]:
        table[col] = pd.to_numeric(table.get(col), errors="coerce").fillna(0)
    table["Nearest"] = pd.to_datetime(table.get("Nearest"), errors="coerce")
    table["BranchGroup"] = table.get("BranchGroup").fillna("")
    expiry_branch_source.data = ColumnDataSource.from_df(table[EXPIRY_BRANCH_FIELDS])

    ranked = by_branch.sort_values(["CRITICAL", "WARNING", "Qty"], ascending=False).head(25)
    expiry_branch_plot_source.data = {"Branch": ranked["BranchName"].tolist(),
                                      **{s: ranked[s].astype(float).tolist() for s in FUTURE_EXPIRY_STATUSES}}
    expiry_branch_plot.y_range.factors = ranked["BranchName"].tolist()[::-1]
    expiry_branch_plot.height = max(260, 20 * len(ranked) + 120)
    expiry_branch_empty.visible = ranked.empty

    items = db.expiry_items(f, branches, statuses)
    items["ExpiryDate"] = pd.to_datetime(items["ExpiryDate"])
    items["StatusLabel"] = items["ExpiryStatus"].map(EXPIRY_LABELS)
    items["Risk"] = np.where(items["AtRisk"].fillna(False).astype(bool), "Тийм", "")
    expiry_items_source.data = ColumnDataSource.from_df(items.drop(columns=["AtRisk", "ExpiryStatus", "StockValue"]))

    info = metadata.get("expiry") or {}
    missing = [listed for listed, data in TRACKED if data is None]
    note = (f"Үлдэгдлийн огноо: <b>{info.get('snapshot_date', '—')}</b> · "
            f"хугацаа уншигдсан мөр: <b>{info.get('expiry_completeness', 0):.1%}</b> · "
            f"хүснэгтэд эхний 3,000 багц (хамгийн ойр дуусахаас эхлэн).")
    if missing:
        note += f"<br>Жагсаалтын {len(missing)} салбар агуулахын өгөгдөлд олдсонгүй: {', '.join(missing)}."
    if not info.get("has_expiry_dates", False):
        note += ("<br><b style='color:#B91C1C;'>Сүүлийн үлдэгдлийн файлд хугацааны багана олдсонгүй</b> "
                 "(«Сери.Хүртэл хүчинтэй», «Огноо», «Date», «Expiry Date», «Хугацаа»).")
    expiry_note.text = f"<div style='font-size:12px;color:#4B5563;padding:4px 0;'>{note}</div>"


def expiry_branch_tap_callback(attr: str, old, new) -> None:
    """Click a branch row to drill into that branch."""
    if not new:
        return
    name = expiry_branch_source.data["BranchName"][new[0]]
    expiry_branch_source.selected.indices = []
    if name in {v for v, _ in expiry_branch.options}:
        expiry_branch.value = name


def current_top_measure(f: Filters) -> str:
    mode = TOP_MODES[top_mode.active]
    return MetricsCalculator.auto_top_measure(f.statuses, f.critical_only) if mode == "auto" else mode


def update_top_chart(f: Filters) -> None:
    measure = current_top_measure(f)
    level = "sku" if top_level.active == 1 else "row"
    items = db.top_items(f, measure, level)
    names = [str(x)[:55] for x in items["SKU_Name"]]
    if level == "sku":
        branches = [f"{int(b)} салбар" for b in items["Branches"]]
        labels = MetricsCalculator.unique_bar_labels(names, [str(i) for i in items["SKU_ID"]])
    else:
        branches = items["BranchName"].astype(object).fillna("").astype(str).tolist()
        labels = MetricsCalculator.unique_bar_labels(names, branches)
    veds = items["VED"].fillna("Тодорхойгүй").astype(str).tolist()
    top_source.data = {
        "Label": labels,
        "Name": names,
        "Branch": branches,
        "VED": veds,
        "Status": items["Status"].astype(object).fillna("—").astype(str).tolist(),
        "Value": items["Value"].astype(float).tolist(),
        "ValueText": [f"{v:,.0f}" for v in items["Value"]],
        "Gap": items["ROPGap"].fillna(0).tolist(),
        "OnHand": items["InventoryPosition"].fillna(0).tolist(),
        "ROP": items["ROP"].fillna(0).tolist(),
        "Color": [VED_COLORS.get(v, "#6B7280") for v in veds],
    }
    shortage_plot.y_range.factors = labels[::-1]
    shortage_plot.x_range.end = float(items["Value"].max()) * 1.12 if len(items) else 1
    title, axis = TOP_TITLES[measure]
    suffix = " · SKU (салбаруудын нийт)" if level == "sku" else " · SKU × салбар"
    shortage_plot.title.text = title + suffix + (" (автомат)" if top_mode.active == 0 else "")
    shortage_plot.xaxis.axis_label = axis
    top_empty.visible = items.empty


def _ratio_text(inventory, rop, band: str) -> str:
    if band in ("NODATA", "NOROP") or not rop or pd.isna(rop):
        return "—"
    ratio = max(float(inventory or 0), 0.0) / float(rop)
    return f"{ratio:.0%}" if ratio < 1 else f"{ratio:.1f}×"


def update_sku_tab(f: Filters) -> None:
    bands = db.coverage_bands(f)
    by_band = bands.groupby("Band")[["Rows", "SKUs", "Gap", "Excess"]].sum()
    shown = [k for k in COVERAGE_BANDS if k not in ("NOROP", "NODATA") or by_band["Rows"].get(k, 0) > 0]
    by_band = by_band.reindex(shown).fillna(0)
    total = float(by_band["Rows"].sum())
    selected = sku_band.value
    band_source.data = {
        "Key": shown,
        "Label": [BANDS[k][0] for k in shown],
        "Rows": by_band["Rows"].tolist(),
        # a distinct SKU count summed over VED classes; a SKU sits in one VED class
        "SKUs": by_band["SKUs"].tolist(),
        "Share": (by_band["Rows"] / total if total else by_band["Rows"] * 0).tolist(),
        "Text": [f"{r:,.0f}  ·  {_share_text(r / total) if total else '0%'}" for r in by_band["Rows"]],
        "Color": [BANDS[k][1] for k in shown],
        "Alpha": [1.0 if not selected or k == selected else 0.3 for k in shown],
        "Desc": [BANDS[k][2] for k in shown],
        "Gap": by_band["Gap"].tolist(),
        "Excess": by_band["Excess"].tolist(),
    }
    band_plot.x_range.factors = [BANDS[k][0] for k in shown]
    band_plot.y_range.end = max(float(by_band["Rows"].max()) * 1.15, 1) if len(by_band) else 1
    band_empty.visible = total == 0

    ved = bands[bands["VED"].isin(VED_CHOICES)].pivot_table(index="VED", columns="Band", values="Rows", aggfunc="sum")
    ved = ved.reindex(index=VED_CHOICES, columns=COVERAGE_BANDS).fillna(0)
    shares = ved.div(ved.sum(axis=1).where(lambda x: x > 0), axis=0).fillna(0)
    for item in ved_band_plot.legend[0].items:  # legend lists only the bands on the chart
        item.visible = item.label.value in {BANDS[k][0] for k in shown}
    ved_band_source.data = {"VED": VED_CHOICES, **{k: shares[k].tolist() for k in COVERAGE_BANDS},
                            **{f"{k}_n": ved[k].tolist() for k in COVERAGE_BANDS}}

    # KPI strip: what needs action (short), how much, and what is tied up (excess)
    rows = by_band["Rows"]
    short = float(rows.reindex(SHORT_BANDS).fillna(0).sum())
    ok = float(rows.get("OK", 0))
    over = float(rows.get("OVER", 0) + rows.get("HIGH", 0))
    v_short = float(ved.loc["V", list(SHORT_BANDS)].sum()) if "V" in ved.index else 0.0
    v_total = float(ved.loc["V"].sum()) if "V" in ved.index else 0.0
    pct = lambda n: _share_text(n / total) if total else "0%"  # noqa: E731
    html = '<div style="display:flex;gap:12px;flex-wrap:wrap;width:100%;box-sizing:border-box;font-family:Segoe UI,Arial,sans-serif;">'
    html += card("ROP-оос доош мөр", f"{short:,.0f}", f"{pct(short)} · нөхөх шаардлагатай", BANDS["LOW"][1])
    html += card("ROP хүртэл дутуу", f"{by_band['Gap'].sum():,.0f}", "ширхэг — захиалах хэмжээ", BANDS["BELOW"][1])
    html += card("V бараа ROP-оос доош", f"{v_short:,.0f}", f"V мөрийн {_share_text(v_short / v_total) if v_total else '0%'}", VED_COLORS["V"])
    html += card("Хэвийн нөөц", f"{ok:,.0f}", f"{pct(ok)} · ROP-ийн 1–2 дахин", BANDS["OK"][1])
    html += card("ROP-оос 2+ дахин их", f"{over:,.0f}", f"{pct(over)} · илүүдэл {by_band['Excess'].sum():,.0f} ширхэг", BANDS["OVER"][1])
    sku_kpis.text = html + "</div>"

    items = db.table(replace(f, band=selected))
    items["StatusColor"] = items["Status"].map(STATUS_COLORS).fillna("#6B7280")
    items["BandColor"] = items["Band"].map(lambda k: BANDS.get(k, ("", "#9CA3AF"))[1])
    ratio = (items["InventoryPosition"].clip(lower=0) / items["ROP"].where(items["ROP"] > 0)).fillna(0)
    items["BarPct"] = (ratio / 3).clip(upper=1).mul(100).round(1)  # full bar = 3x ROP
    items["Supplier"] = items["Supplier"].replace("", np.nan).fillna("—")
    items["RatioText"] = [_ratio_text(i, r, b) for i, r, b in zip(items["InventoryPosition"], items["ROP"], items["Band"])]
    table_source.data = ColumnDataSource.from_df(items)
    table_title.text = (
        "<div style='margin:6px 0 2px 0;padding-top:14px;border-top:1px solid #E5E7EB;font-size:15px;font-weight:700;color:#12263A;'>"
        f"Жагсаалт — {BANDS[selected][0] if selected else 'бүх бүлэг'}"
        "<span style='font-weight:400;font-size:12px;color:#6B7280;'> · хамгийн яаралтай нь эхэнд, эхний 1,000 мөр · мөр дээр дарж дэлгэрэнгүйг харна</span></div>"
    )


def band_tap_callback(attr: str, old, new) -> None:
    """Click a band to list its rows; click it again to show every band."""
    if not new:
        return
    key = band_source.data["Key"][new[0]]
    band_source.selected.indices = []
    sku_band.value = "" if sku_band.value == key else key


def update_branch_chart() -> None:
    """Every active branch in list order (branches without ROP rows at 0)."""
    group = db.branch_exposure()
    if TRACKED_NAMES:
        group = group.set_index("Branch").reindex(list(TRACKED_NAMES)).fillna(0).rename_axis("Branch").reset_index()
    total = group["ROP"].where(group["ROP"] > 0)
    for v in "VED":
        group[f"{v}Share"] = (group[f"{v}ROP"] / total).fillna(0)
    labels = group["Branch"].astype(str).tolist()
    branch_source.data = {col: group[col].tolist() for col in branch_source.data}
    branch_plot.y_range.factors = labels[::-1]
    branch_plot.height = max(400, 20 * len(labels) + 120)


def _pct_text(value: float) -> str:
    return "—" if value is None or pd.isna(value) else f"{value:.1%}"


def update_coverage(f: Filters) -> None:
    level = COVERAGE_LEVELS[coverage_level.active]
    metric = coverage_metric.value
    df = MetricsCalculator.finalize_coverage(db.coverage_summary(f, level))
    grand = df[df["TotalLevel"] == df["TotalLevel"].max()] if len(df) else df
    total = grand.iloc[0] if len(grand) and grand.iloc[0]["EligibleRows"] > 0 else None
    groups = df[df["TotalLevel"] == 0]
    df["Value"] = df[metric]

    # KPI strip: all three coverage measures for the whole filtered selection
    html = '<div style="display:flex;gap:12px;flex-wrap:wrap;width:100%;box-sizing:border-box;font-family:Segoe UI,Arial,sans-serif;">'
    for key, short in COVERAGE_SHORT.items():
        accent = "#1F4E5F" if key == metric else "#D1D5DB"
        html += card(f"НИЙТ {short}", _pct_text(total[key]) if total is not None else "—", COVERAGE_METRICS[key].split(" — ")[1], accent)
    if total is not None:
        html += card("ROP-той мөр", f"{total['EligibleRows']:,.0f}", f"хангагдсан {total['CoveredRows']:,.0f}", "#0F766E")
        html += card("Дутуу (ROP хүртэл)", f"{total['Gap']:,.0f}", "Σ (ROP − хангагдсан)", "#F97316")
    coverage_kpis.text = html + "</div>"

    table = pd.concat([grand, groups.sort_values(metric, na_position="last")], ignore_index=True)
    coverage_table.columns = COVERAGE_GROUP_COLUMNS[level] + COVERAGE_MEASURE_COLUMNS
    coverage_table_source.data = ColumnDataSource.from_df(table.drop(columns=["TotalLevel", "RatioSum", "Value"], errors="ignore"))

    heat = level == "branch_category"
    coverage_plot.visible = heat
    coverage_bar_plot.visible = not heat
    if heat:
        update_coverage_heatmap(df)
    else:
        update_coverage_bars(groups, level, metric, total)


def update_coverage_heatmap(df: pd.DataFrame) -> None:
    cells = df[df["EligibleRows"] > 0] if len(df) else df
    is_total = (cells["BranchName"] == TOTAL) | (cells["Category"] == TOTAL)
    coverage_source.data = {
        "Branch": cells["BranchName"].tolist(),
        "Category": cells["Category"].tolist(),
        "Value": cells["Value"].tolist(),
        **{c: cells[c].tolist() for c in ["AvgCoverage", "CoveredShare", "FillRate", "EligibleRows", "CoveredRows", "SKUCount", "ROP", "Filled", "Gap"]},
        "Line": ["#111827" if t else "#FFFFFF" for t in is_total],
        "LineWidth": [1.2 if t else 0.5 for t in is_total],
    }
    totals = cells[is_total]
    coverage_total_text.data = {
        "Branch": totals["BranchName"].tolist(),
        "Category": totals["Category"].tolist(),
        "Text": [f"{v:.0%}" for v in totals["Value"]],
    }
    order = db.filter_options()["categories"]
    present = set(cells["Category"]) - {TOTAL}
    coverage_plot.x_range.factors = [c for c in order if c in present] + sorted(present - set(order)) + ([TOTAL] if len(cells) else [])
    branch_rop = cells[(cells["Category"] == TOTAL) & (cells["BranchName"] != TOTAL)].sort_values("ROP")
    coverage_plot.y_range.factors = branch_rop["BranchName"].tolist() + ([TOTAL] if len(cells) else [])
    coverage_plot.height = max(360, 16 * len(coverage_plot.y_range.factors) + 140)
    coverage_empty.visible = cells.empty


def update_coverage_bars(groups: pd.DataFrame, level: str, metric: str, total) -> None:
    bars = groups[groups["EligibleRows"] > 0].copy()
    bars["Value"] = bars[metric]
    bars = bars.sort_values([metric, "ROP"], ascending=[True, False])
    if level == "sku":
        bars = bars.head(40)
        labels = MetricsCalculator.unique_bar_labels([str(n)[:50] for n in bars["SKU_Name"]], [str(i) for i in bars["SKU_ID"]])
        title = "Хангалт хамгийн муу 40 SKU (бүх салбарын нийт; ижил бол ROP ихийг эхэнд)"
    else:
        column_name = "BranchName" if level == "branch" else "Category"
        labels = bars[column_name].astype(str).tolist()
        title = "Хангалт — салбараар (муу нь дээрээ)" if level == "branch" else "Хангалт — категориор (муу нь дээрээ)"
    coverage_bar_source.data = {
        "Label": labels,
        "Value": bars["Value"].tolist(),
        "ValueText": [_pct_text(v) for v in bars["Value"]],
        **{c: bars[c].tolist() for c in ["AvgCoverage", "CoveredShare", "FillRate", "EligibleRows", "CoveredRows", "SKUCount", "BranchCount", "ROP", "Filled", "Gap"]},
    }
    coverage_bar_plot.y_range.factors = labels[::-1]
    coverage_bar_plot.height = max(260, 22 * len(labels) + 110)
    coverage_bar_plot.title.text = f"{title} · {COVERAGE_SHORT[metric]}"
    if total is not None:
        coverage_total_span.location = float(total[metric])
        coverage_total_label.x = float(total[metric])
        coverage_total_label.text = f"НИЙТ {total[metric]:.1%}"
    coverage_total_span.visible = coverage_total_label.visible = total is not None
    coverage_bar_empty.visible = bars.empty


def update_quality() -> None:
    inv = state["inventory"]
    mapping = state.get("mapping_stats", {})
    qm = db.quality()
    negative = int((pd.to_numeric(inv["OnHand"], errors="coerce") < 0).sum()) if len(inv) and "OnHand" in inv else 0
    metrics = [
        ("Matched rows", mapping.get("matched_rows", 0), f"{mapping.get('mapping_rate_rows', 0):.1%}", "#0F766E"),
        ("Unmatched rows", mapping.get("unmatched_rows", 0), f"{mapping.get('unmatched_rows', 0):,.0f}", "#F97316"),
        ("Missing inventory SKU", qm["missing_inventory"], "SKU", "#7C3AED"),
        ("Missing ROP SKU", qm["missing_rop_sku"], "SKU", "#6B7280"),
        ("Low-confidence ROP", qm["low_confidence_sku"], "SKU", "#F59E0B"),
        ("Negative inventory", negative, "rows", "#B91C1C"),
    ]
    quality_source.data = {
        "Metric": [m[0] for m in metrics],
        "Value": [m[1] for m in metrics],
        "Label": [m[2] for m in metrics],
        "Color": [m[3] for m in metrics],
    }
    quality_plot.x_range.factors = [m[0] for m in metrics]


def update_review_table() -> None:
    review = state["mapping_review"].copy()
    cols = ["InventoryName", "OnHand", "BranchName", "Suggested_SKU_ID", "Suggested_SKU_Name", "Similarity", "Decision", "NormalizedKey"]
    for col in cols:
        if col not in review:
            review[col] = np.nan
    review_source.data = ColumnDataSource.from_df(review[cols].head(2000))


def _date_label(date_key: int, scope: str) -> str:
    label = pd.Timestamp(str(date_key)).strftime("%Y-%m-%d")
    return f"{label} (сүлжээ)" if scope == "NETWORK" else label


def update_movement_options() -> None:
    dates = db.snapshot_dates()
    opts = [(str(k), _date_label(k, s)) for k, s in zip(dates["DateKey"], dates["Scope"])]
    branch_opts = [o for o, s in zip(opts, dates["Scope"]) if s == "BRANCH"] or opts
    movement_from.options = opts
    movement_to.options = opts
    keys = [o[0] for o in branch_opts]
    if movement_to.value not in {o[0] for o in opts}:
        movement_to.value = keys[-1] if keys else ""
    if movement_from.value not in {o[0] for o in opts}:
        movement_from.value = keys[-2] if len(keys) > 1 else (keys[0] if keys else "")


def update_trend(f: Filters) -> None:
    wide = MetricsCalculator.trend_table(db.trend(f, bool(trend_include_network.active)))
    labels = [_date_label(k, s) for k, s in zip(wide["DateKey"], wide["Scope"])]
    total = wide["Total"].where(wide["Total"] > 0)
    data = {
        "DateLabel": labels,
        "Risk": wide[list(RISK_STATUSES)].sum(axis=1).tolist() if len(wide) else [],
        "Critical": wide["Critical"].tolist(),
        "Gap": wide["Gap"].tolist(),
        "Total": wide["Total"].tolist(),
    }
    for s in STATUS_ORDER:
        data[s] = (wide[s] / total).fillna(0).tolist() if len(wide) else []
    trend_mix_plot.x_range.factors = labels
    trend_source.data = data

    if len(wide) >= 2:
        last, prev = wide.iloc[-1], wide.iloc[-2]
        risk_now, risk_prev = last[list(RISK_STATUSES)].sum(), prev[list(RISK_STATUSES)].sum()
        html = '<div style="display:flex;gap:12px;flex-wrap:wrap;width:100%;box-sizing:border-box;font-family:Segoe UI,Arial,sans-serif;">'
        html += card("Эрсдэлтэй SKU × салбар", f"{risk_now:,.0f}", f"Өмнөх snapshot-оос {MetricsCalculator.delta(risk_now, risk_prev)}", STATUS_COLORS["ROP-оос доош"])
        html += card("Critical V/E", f"{last['Critical']:,.0f}", f"Өмнөх snapshot-оос {MetricsCalculator.delta(last['Critical'], prev['Critical'])}", VED_COLORS["V"])
        html += card("ROP gap", f"{last['Gap']:,.0f}", f"Өмнөх snapshot-оос {MetricsCalculator.delta(last['Gap'], prev['Gap'])}", "#F97316")
        html += card("Эрсдэлийн хувь", f"{last['RiskShare']:.1%}", f"Өмнө {prev['RiskShare']:.1%}", "#1F4E5F")
        html += "</div>"
        trend_summary.text = html
    else:
        trend_summary.text = "<i>Тренд харуулахад дор хаяж 2 snapshot хэрэгтэй.</i>"
    update_movement(f)


def update_movement(f: Filters) -> None:
    if not movement_from.value or not movement_to.value or movement_from.value == movement_to.value:
        movement_source.data = ColumnDataSource.from_df(pd.DataFrame(columns=[c.field for c in movement_columns]))
        movement_summary_div.text = "<i>Хоёр өөр огноо сонгоно уу.</i>"
        return
    a, b = int(movement_from.value), int(movement_to.value)
    summary = db.movement_summary(f, a, b)
    movement_source.data = ColumnDataSource.from_df(db.movement(f, a, b))
    if summary["compared"] == 0:
        movement_summary_div.text = "<i>Хоёр snapshot-д давхцах SKU × салбар мөр алга (сүлжээ ба салбарын scope холилдсон байж магадгүй).</i>"
        return
    movement_summary_div.text = (
        "<div style='font-family:Segoe UI,Arial,sans-serif;font-size:13px;color:#374151;padding:6px 0;'>"
        f"<b>{summary['compared']:,}</b> мөрийг харьцуулснаас "
        f"<b style='color:#B91C1C;'>{summary['worse']:,} муудсан</b>, "
        f"<b style='color:#0F766E;'>{summary['better']:,} сайжирсан</b>, "
        f"нийт {summary['changed']:,} мөрийн status өөрчлөгдсөн. Хүснэгтэд эхний 2,000 мөр (муудсанаас эхлэн)."
        "</div>"
    )


def refresh_all() -> None:
    f = current_filters()
    update_kpis(f)
    update_status_chart(f)
    update_ved_chart(f)
    update_top_chart(f)
    update_sku_tab(f)
    update_coverage(f)
    update_class_tab(f)
    update_expiry(f)
    update_quality()
    update_review_table()
    update_freshness()
    update_trend(f)


def reset_branch_filter() -> None:
    branch_filter.options = ["ALL"] + branch_options()
    branch_filter.value = "ALL"
    min_gap_slider.end = max(1000, db.max_gap())


def rebuild_from_inventory(inv_df: pd.DataFrame, source_file: str, mapping_review: pd.DataFrame | None = None, mapping_stats: dict | None = None) -> None:
    global dim_branch
    date_value = snapshot_picker.value or metadata.get("snapshot_date", date.today().isoformat())
    rop_wide = branch_rop_wide(fact_branch_rop, dim_branch, dim_sku)
    status = build_status_snapshot(dim_sku, inv_df, rop_wide, date_value, excess_slider.value)
    if missing_as_zero.active:
        status = apply_missing_as_zero(status)
    new_dim_branch = ensure_branches(dim_branch, status)
    if len(new_dim_branch) != len(dim_branch):
        dim_branch = new_dim_branch
        db.replace_table("dim_branch", dim_branch)
    db.replace_table("fact_sku_status", to_fact_status(status))
    state["inventory"] = inv_df
    state["status_wide"] = status
    state["mapping_review"] = mapping_review if mapping_review is not None else pd.DataFrame()
    state["mapping_stats"] = mapping_stats or {}
    state["source_file"] = source_file
    reset_branch_filter()
    refresh_all()


def save_upload_to_history(snapshot_date: str) -> None:
    """Persist the uploaded snapshot so it shows up in the Trend tab (now and after restart)."""
    fact = db.table_frame("fact_sku_status")
    history = upsert_history(db.table_frame("fact_status_history"), fact)
    date_keys = sorted(set(history["DateKey"].astype(int)))
    dim_date = build_dim_date(date_keys)
    write_tables({"fact_status_history": history, "dim_branch": dim_branch, "dim_date": dim_date})
    db.replace_table("dim_date", dim_date)
    db.replace_table("fact_status_history", history)
    update_movement_options()
    new_key = str(pd.Timestamp(snapshot_date).strftime("%Y%m%d"))
    earlier = [key for key, _ in movement_to.options if key < new_key]
    movement_to.value = new_key
    if earlier:
        movement_from.value = earlier[-1]


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


def update_batches(inventory: pd.DataFrame, snapshot_date: str) -> None:
    """Rebuild fact_batch_expiry (Хугацаат tab) from uploaded inventory rows."""
    batches = build_batch_expiry(inventory, dim_sku, fact_branch_rop, snapshot_date, ExpiryConfig.load())
    db.replace_table("fact_batch_expiry", batches)
    dated = inventory["ExpiryDate"].notna() if "ExpiryDate" in inventory else pd.Series(False, index=inventory.index)
    metadata["expiry"] = {"snapshot_date": snapshot_date, "has_expiry_dates": bool(dated.any()),
                          "expiry_completeness": float(dated.mean()) if len(dated) else 0.0}


def upload_callback(attr: str, old: str, new: str) -> None:
    if not new:
        return
    try:
        payload = base64.b64decode(new)
        inventory, parse_meta = parse_inventory(payload, filename=upload.filename)
        inventory["SnapshotDate"] = pd.to_datetime(inventory["SnapshotDate"], errors="coerce").fillna(pd.Timestamp(snapshot_picker.value))
        inventory["SourceFile"] = upload.filename
        mapped, review, stats = map_inventory(inventory, alias_mapping, dim_sku, dim_branch)
        update_batches(mapped, str(snapshot_picker.value))
        rebuild_from_inventory(mapped, upload.filename, review, stats)
        save_snapshot_files(mapped, state["status_wide"], review, snapshot_picker.value)
        save_upload_to_history(str(snapshot_picker.value))
        update_trend(current_filters())
        state["uploaded"] = True  # live refresh must not replace what was uploaded
        message.text = f"<span style='color:#0F766E;font-weight:700;'>Амжилттай:</span> {upload.filename} | {stats['matched_rows']:,}/{stats['inventory_rows']:,} мөр таарсан ({stats['mapping_rate_rows']:.1%}) | Scope: {stats['scope']}"
    except Exception as exc:
        message.text = f"<span style='color:#B91C1C;font-weight:700;'>Алдаа:</span> {friendly_error(exc)}"


def reset_callback() -> None:
    state["uploaded"] = False
    db.replace_table("fact_sku_status", initial_status)
    if initial_batches is not None:
        db.replace_table("fact_batch_expiry", initial_batches)
    metadata["expiry"] = initial_expiry_meta
    state["inventory"] = initial_inventory
    state["status_wide"] = None
    state["mapping_review"] = initial_mapping_review.copy()
    state["source_file"] = metadata.get("source_files", {}).get("inventory", "Initial snapshot")
    state["mapping_stats"] = metadata.get("mapping", {})
    reset_branch_filter()
    message.text = "Эхний snapshot сэргээгдлээ."
    refresh_all()


def clear_filters_callback() -> None:
    status_filter.value = []
    ved_filter.value = []
    category_filter.value = []
    abc_filter.value = []
    xyz_filter.value = []
    expiry_status.value = []
    sku_band.value = ""
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

    master = db.sku_detail(int(sku_id)) if sku_id is not None and not pd.isna(sku_id) else {}
    lead = master.get("LeadTime_Months")
    demand = master.get("AvgMonthlyDemand")
    rop_src = master.get("ROP_Source")

    sc = STATUS_COLORS.get(status, "#6B7280")
    cov_str = f"{cov:.1%}" if cov is not None and not pd.isna(cov) else "—"
    supplier_str = supplier if supplier is not None and not pd.isna(supplier) and supplier != "" else "—"
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
        f'<div style="font-size:12px;color:#6B7280;margin-top:10px;">Нийлүүлэгч: {supplier_str} · ROP source: {rop_src or "—"} · Confidence: {conf or "—"}</div>'
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
    if sku_id is None or pd.isna(sku_id) or str(sku_id) in {"", "<NA>", "nan"}:
        review_message.text = "<span style='color:#B91C1C;'>Энэ мөрөнд санал болгосон SKU байхгүй.</span>"
        return
    global alias_mapping
    new_row = pd.DataFrame([{
        "NormalizedKey": str(key),
        "SKU_ID": int(float(sku_id)),
        "MasterName": str(sku_name),
        "MappingType": "Manual - dashboard review",
        "Notes": "Accepted in dashboard",
    }])
    # One alias per normalized key: a new decision replaces an older one.
    updated = pd.concat([alias_mapping[alias_mapping["NormalizedKey"] != str(key)], new_row], ignore_index=True)
    try:
        write_tables({"alias_mapping": updated}, refresh_db=False)
    except Exception as exc:
        review_message.text = f"<span style='color:#B91C1C;'>Хадгалахад алдаа: {exc}</span>"
        return
    alias_mapping = updated
    review_source.patch({"Decision": [(idx, "Зөвшөөрсөн")]})
    review_source.selected.indices = []
    review_message.text = f"<span style='color:#0F766E;font-weight:700;'>Зөвшөөрөв:</span> '{str(key)[:40]}' → SKU {int(float(sku_id))} alias-д нэмэгдлээ."


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


def trend_callback(attr: str, old, new) -> None:
    update_trend(current_filters())


def movement_callback(attr: str, old, new) -> None:
    update_movement(current_filters())


upload.on_change("value", upload_callback)
reset_button.on_click(reset_callback)
clear_filters_button.on_click(clear_filters_callback)
accept_button.on_click(accept_mapping_callback)
reject_button.on_click(reject_mapping_callback)
excess_slider.on_change("value", threshold_callback)
missing_as_zero.on_change("active", threshold_callback)
critical_only.on_change("active", filter_callback)
table_source.selected.on_change("indices", table_selected_callback)
trend_include_network.on_change("active", trend_callback)
movement_from.on_change("value", movement_callback)
movement_to.on_change("value", movement_callback)
for widget in [status_filter, ved_filter, category_filter, branch_filter, search_input, min_gap_slider]:
    widget.on_change("value", filter_callback)

def status_tap_callback(attr: str, old, new) -> None:
    """Click a status bar to filter to it; click it again to clear."""
    if not new:
        return
    status = status_source.data["Status"][new[0]]
    status_source.selected.indices = []
    if status not in STATUS_CHOICES:
        return
    status_filter.value = [] if status_filter.value == [status] else [status]


def heatmap_tap_callback(source: ColumnDataSource, class_filter: MultiChoice):
    """Click a class x Status cell to filter to that class and status; click
    it again to clear both."""
    def callback(attr: str, old, new) -> None:
        if not new:
            return
        cls, status = source.data["Class"][new[0]], source.data["Status"][new[0]]
        source.selected.indices = []
        if status not in STATUS_CHOICES:
            return
        again = class_filter.value == [cls] and status_filter.value == [status]
        class_filter.value = [] if again else [cls]
        status_filter.value = [] if again else [status]
    return callback


status_source.selected.on_change("indices", status_tap_callback)
ved_source.selected.on_change("indices", heatmap_tap_callback(ved_source, ved_filter))
abc_source.selected.on_change("indices", heatmap_tap_callback(abc_source, abc_filter))
xyz_source.selected.on_change("indices", heatmap_tap_callback(xyz_source, xyz_filter))
for widget in [abc_filter, xyz_filter]:
    widget.on_change("value", lambda attr, old, new: update_class_tab(current_filters()))
class_order.on_change("active", lambda attr, old, new: update_class_tab(current_filters()))
sku_band.on_change("value", lambda attr, old, new: update_sku_tab(current_filters()))
band_source.selected.on_change("indices", band_tap_callback)
for widget in [expiry_branch, expiry_status]:
    widget.on_change("value", lambda attr, old, new: update_expiry(current_filters()))
expiry_branch_source.selected.on_change("indices", expiry_branch_tap_callback)
top_mode.on_change("active", lambda attr, old, new: update_top_chart(current_filters()))
top_level.on_change("active", lambda attr, old, new: update_top_chart(current_filters()))
coverage_level.on_change("active", lambda attr, old, new: update_coverage(current_filters()))
coverage_metric.on_change("value", lambda attr, old, new: update_coverage(current_filters()))

# Browser-side CSV export from the currently filtered table source.
CSV_EXPORT_JS = """
const data = source.data;
const columns = Object.keys(data).filter(k => k !== 'index');
let csv = columns.join(',') + '\\n';
const n = data[columns[0]] ? data[columns[0]].length : 0;
for (let i = 0; i < n; i++) {
  const row = columns.map(c => {
    const value = data[c][i] == null ? '' : String(data[c][i]);
    return '"' + value.replaceAll('"', '""') + '"';
  });
  csv += row.join(',') + '\\n';
}
const blob = new Blob(['\\ufeff' + csv], {type: 'text/csv;charset=utf-8;'});
const link = document.createElement('a');
link.href = URL.createObjectURL(blob);
link.download = filename;
link.click();
URL.revokeObjectURL(link.href);
"""
download_button = Button(label="Шүүсэн хүснэгт CSV", button_type="success", width=180)
download_button.js_on_click(CustomJS(args=dict(source=table_source, filename="rop_filtered.csv"), code=CSV_EXPORT_JS))
movement_download = Button(label="Өөрчлөлтийн хүснэгт CSV", button_type="success", width=200)
coverage_download = Button(label="Хангалтын хүснэгт CSV", button_type="success", width=190)
coverage_download.js_on_click(CustomJS(args=dict(source=coverage_table_source, filename="rop_coverage.csv"), code=CSV_EXPORT_JS))
movement_download.js_on_click(CustomJS(args=dict(source=movement_source, filename="rop_status_movement.csv"), code=CSV_EXPORT_JS))
expiry_download = Button(label="Хугацаат хүснэгт CSV", button_type="success", width=190)
expiry_download.js_on_click(CustomJS(args=dict(source=expiry_items_source, filename="rop_expiry_batches.csv"), code=CSV_EXPORT_JS))
class_download = Button(label="ABC / XYZ хүснэгт CSV", button_type="success", width=190)
class_download.js_on_click(CustomJS(args=dict(source=class_items_source, filename="rop_abc_xyz.csv"), code=CSV_EXPORT_JS))

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
filters = row(status_filter, ved_filter, category_filter, branch_filter, search_input, min_gap_slider, sizing_mode="stretch_width")

def section_title(text: str) -> Div:
    return Div(text=f"<div style='margin:6px 0 2px 0;padding-top:14px;border-top:1px solid #E5E7EB;font-size:15px;font-weight:700;color:#12263A;'>{text}</div>", sizing_mode="stretch_width")


executive_panel = TabPanel(
    title="Удирдлагын тойм",
    child=column(
        kpi_container,
        section_title("Эрсдэлийн тойм"),
        Div(text="<span style='font-size:12px;color:#6B7280;'>Status баганан эсвэл VED × Status нүдэн дээр дарж шүүнэ (дахин дарвал цэвэрлэнэ). Саарал өнгө — өгөгдлийн чанарын status.</span>", sizing_mode="stretch_width"),
        gridplot([[status_plot, ved_plot]], sizing_mode="stretch_width", toolbar_location=None),
        section_title("TOP 20"),
        row(Div(text="<span style='font-size:12px;color:#374151;'>Эрэмбэ:</span>", width=50), top_mode,
            Div(text="<span style='font-size:12px;color:#374151;'>Түвшин:</span>", width=55), top_level),
        shortage_plot,
        sizing_mode="stretch_width",
    ),
)
analytics_panel = TabPanel(
    title="SKU анализ",
    child=column(
        Div(text=(
            "<div style='padding:10px 0;color:#374151;font-size:13px;line-height:1.5;'>"
            "<b>SKU анализ</b> — SKU × салбарын мөр бүрийн үлдэгдлийг ROP-той нь харьцуулна: "
            "<b style='color:#DC2626;'>улаан</b> — ROP-оос доош (нөхөх), <b style='color:#16A34A;'>ногоон</b> — хэвийн, "
            "<b style='color:#1D4ED8;'>цэнхэр</b> — илүүдэл. Баганан дээр дарж доорх жагсаалтыг тухайн бүлгээр шүүнэ "
            "(дахин дарвал цэвэрлэнэ). Дээрх шүүлтүүрүүд энд мөн үйлчилнэ."
            "</div>"
        ), sizing_mode="stretch_width"),
        sku_kpis,
        gridplot([[band_plot, ved_band_plot]], sizing_mode="stretch_width", toolbar_location=None),
        table_title,
        row(sku_band),
        detail_panel,
        main_table,
        sizing_mode="stretch_width",
    ),
)
branch_panel = TabPanel(
    title="Салбарын VED ангилал бүтэц",
    child=column(
        Div(text=(
            "<div style='padding:10px 0;color:#374151;font-size:13px;line-height:1.5;'>"
            "<b>Салбарын VED ангилал бүтэц</b> — «Салбарын жагсаалт»-ын салбар бүрийн ROP-ийг VED ангиллаар (V, E, D) задлана; "
            "салбарууд жагсаалтын дарааллаар. Хулганаа баганан дээр аваачиж хувийг харна."
            "</div>"
        ), sizing_mode="stretch_width"),
        branch_plot,
        sizing_mode="stretch_width",
    ),
)
coverage_panel = TabPanel(
    title="Барааны тархац",
    child=column(
        Div(text=(
            "<div style='padding:10px 0;color:#374151;font-size:13px;line-height:1.5;'>"
            "<b>Барааны тархац</b> — ROP-той SKU × салбарын мөр бүрийн үлдэгдэл ROP-оо хэр хангаж буйг харуулна "
            "(үлдэгдлийн мэдээлэлгүй мөрийг 0 үлдэгдэлтэй гэж тооцно). "
            "<b>НИЙТ</b> нь сонгосон шүүлтүүрийн бүх мөрийн нийлбэр; хүснэгтийн эхний мөр, дулааны зургийн НИЙТ мөр/багана. "
            "Бүлэглэлт болон үзүүлэлтийг доороос сонгоно; дээрх шүүлтүүрүүд энд мөн үйлчилнэ."
            "</div>"
        ), sizing_mode="stretch_width"),
        row(Div(text="<span style='font-size:12px;color:#374151;'>Бүлэглэлт:</span>", width=75), coverage_level,
            coverage_metric, coverage_download),
        coverage_kpis,
        coverage_plot,
        coverage_bar_plot,
        coverage_table,
        sizing_mode="stretch_width",
    ),
)
class_panel = TabPanel(
    title="ABC / XYZ",
    child=column(
        Div(text=(
            "<div style='padding:10px 0;color:#374151;font-size:13px;line-height:1.5;'>"
            "<b>ABC / XYZ</b> — SKU × салбарын мөрийг ROP файлын ангиллаар ABC ба XYZ-ээр тус тусад нь харуулна. "
            "<b>ABC</b> — борлуулалтын дүнгээр: A нь борлуулалтын дийлэнхийг бүрдүүлдэг, C нь хамгийн бага. "
            "<b>XYZ</b> — эрэлтийн тогтвортой байдлаар: X тогтвортой, Y хэлбэлзэлтэй, Z хамгийн тогтворгүй. "
            "Нэг SKU салбар бүрт өөр ангилалтай байж болно; ROP файлд ангилалгүй мөрийг «ABC XYZ.xlsx»-ээс нөхнө (багана «Ангиллын эх»), аль алинд нь байхгүй бол «Тодорхойгүй».<br>"
            "Нүдэн дээр дарж тухайн ангилал ба status-аар шүүнэ (дахин дарвал цэвэрлэнэ). "
            "ABC ба XYZ шүүлтүүр зөвхөн энэ табад үйлчилнэ; дээрх шүүлтүүрүүд энд мөн үйлчилнэ."
            "</div>"
        ), sizing_mode="stretch_width"),
        row(abc_filter, xyz_filter),
        row(column(abc_plot, abc_summary, sizing_mode="stretch_width"),
            column(xyz_plot, xyz_summary, sizing_mode="stretch_width"),
            sizing_mode="stretch_width"),
        section_title("SKU × салбарын жагсаалт"),
        row(Div(text="<span style='font-size:12px;color:#374151;'>Эрэмбэ:</span>", width=55), class_order,
            Div(text="<span style='font-size:12px;color:#6B7280;'>Эхний 2,000 мөр; ангилал дотроо хамгийн яаралтай нь эхэнд. "
                     "Баганын гарчиг дээр дарж эрэмбэлнэ.</span>", width=520),
            class_download),
        class_items_table,
        sizing_mode="stretch_width",
    ),
)
expiry_panel = TabPanel(
    title="Хугацаат",
    child=column(
        Div(text=(
            "<div style='padding:10px 0;color:#374151;font-size:13px;line-height:1.5;'>"
            "<b>Хугацаат</b> — хүчинтэй хугацаа нь дуусаагүй (өнөөдрөөс хойш) үлдэгдэлтэй бүх SKU-ийн багц. "
            "Хугацааг үлдэгдлийн файлын «Сери.Хүртэл хүчинтэй», «Огноо», «Date», «Expiry Date» эсвэл «Хугацаа» баганаас уншина. "
            "Салбарын хяналт «Салбарын жагсаалт»-ын дарааллаар; мөр дээр дарж тухайн салбарыг задална. "
            "Бүлэг: хугацаа дуусахад үлдсэн хоногоор (expiry_config.json). "
            "Дээрх VED, категори, хайлт энд мөн үйлчилнэ; ROP-ийн шүүлтүүр (status, салбар, gap) үйлчлэхгүй."
            "</div>"
        ), sizing_mode="stretch_width"),
        row(expiry_branch, expiry_status, expiry_download),
        expiry_note,
        expiry_kpis,
        expiry_month_plot,
        section_title("Салбарын хяналт"),
        expiry_branch_table,
        expiry_branch_plot,
        section_title("Багцын жагсаалт"),
        expiry_items_table,
        sizing_mode="stretch_width",
    ),
)
trend_panel = TabPanel(
    title="Тренд",
    child=column(
        Div(text=(
            "<div style='padding:10px 0;color:#374151;'>"
            "<b>Тренд</b> нь хадгалсан snapshot бүрийн status-ийг огноогоор харьцуулна. "
            "Дээрх шүүлтүүрүүд (категори, VED, салбар, хайлт) энд мөн үйлчилнэ. "
            "Легенд дээр дарж status-ийг нууж/харуулна."
            "</div>"
        ), sizing_mode="stretch_width"),
        trend_include_network,
        trend_summary,
        trend_mix_plot,
        trend_risk_plot,
        Div(text="<div style='margin:6px 0 2px 0;padding-top:14px;border-top:1px solid #E5E7EB;font-size:15px;font-weight:700;color:#12263A;'>Status-ийн өөрчлөлт: хоёр snapshot-ийн харьцуулалт</div>", sizing_mode="stretch_width"),
        row(movement_from, movement_to, movement_download),
        movement_summary_div,
        movement_table,
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
# Each tab title as its own box; the open tab filled with the header colour.
TAB_BOXES = InlineStyleSheet(css="""
.bk-header { gap: 6px; padding: 4px 0 8px 0; border-bottom: none !important; flex-wrap: wrap; }
.bk-tab {
  background-color: #FFFFFF;
  border: 1px solid #CBD5E1 !important;
  border-radius: 8px !important;
  padding: 7px 14px !important;
  color: #1F2937;
  font-weight: 600;
  box-shadow: 0 1px 2px rgba(18, 38, 58, 0.06);
}
.bk-tab:hover { background-color: #EEF4F7; border-color: #94A3B8 !important; }
.bk-tab.bk-active { background-color: #1F4E5F; border-color: #1F4E5F !important; color: #FFFFFF; }
""")
tabs = Tabs(tabs=[executive_panel, analytics_panel, branch_panel, coverage_panel, class_panel, expiry_panel, trend_panel, quality_panel, method_panel],
            sizing_mode="stretch_width", stylesheets=[TAB_BOXES])

# --------------------------------------------------------------------------
# Live refresh: every minute, reload when the ETL (Dagster, update_rop.py,
# refresh_inventory.py) has rewritten the warehouse or the branch list changed.
# Filters and the open tab stay as they are.
# --------------------------------------------------------------------------
LIVE_CHECK_SECONDS = 60
SETTLE_SECONDS = 20  # an ETL run writes several files; wait until it is done


def data_signature() -> int:
    """Newest modification time among the warehouse files and the branch list."""
    paths = [*DATA_DIR.glob("*.parquet"), DATA_DIR / "metadata.json", BRANCH_LIST_PATH]
    return max((p.stat().st_mtime_ns for p in paths if p.exists()), default=0)


def _live_text(prefix: str) -> str:
    return f"● {prefix} {datetime.now():%H:%M} · {LIVE_CHECK_SECONDS} сек тутам шалгана"


def reload_data() -> None:
    """Re-read every warehouse table and redraw every tab."""
    global metadata, dim_sku, dim_branch, fact_branch_rop, alias_mapping, initial_status, initial_inventory
    global initial_mapping_review, initial_batches, initial_expiry_meta, TRACKED, TRACKED_NAMES
    db.replace_connection(connect(DATA_DIR))
    metadata = json.loads((DATA_DIR / "metadata.json").read_text(encoding="utf-8"))
    dim_sku, dim_branch = db.table_frame("dim_sku"), db.table_frame("dim_branch")
    fact_branch_rop, alias_mapping = db.table_frame("fact_branch_rop"), db.table_frame("alias_mapping")
    initial_status, initial_inventory = db.table_frame("fact_sku_status"), db.table_frame("fact_inventory_snapshot")
    initial_mapping_review = db.table_frame("mapping_review")
    initial_batches = db.table_frame("fact_batch_expiry") if "fact_batch_expiry" in db.tables() else None
    initial_expiry_meta = metadata.get("expiry")
    TRACKED = tracked_branches()
    TRACKED_NAMES = tuple(dict.fromkeys(data for _, data in TRACKED if data))
    db.set_active_branches(TRACKED_NAMES)
    state.update(inventory=initial_inventory, status_wide=None, mapping_review=initial_mapping_review.copy(),
                 source_file=metadata.get("source_files", {}).get("inventory", "Initial snapshot"),
                 mapping_stats=metadata.get("mapping", {}))
    snapshot_picker.value = metadata.get("snapshot_date", snapshot_picker.value)

    # keep each filter's selection where it still exists
    categories = db.filter_options()["categories"]
    category_filter.options = categories
    category_filter.value = [c for c in category_filter.value if c in categories]
    branch = branch_filter.value
    branch_filter.options = ["ALL"] + branch_options()
    branch_filter.value = branch if branch in branch_filter.options else "ALL"
    expiry_branch.options = expiry_branch_options()
    if expiry_branch.value not in {v for v, _ in expiry_branch.options}:
        expiry_branch.value = EXPIRY_LIST
    min_gap_slider.end = max(1000, db.max_gap())

    update_branch_chart()
    update_movement_options()
    refresh_all()


def check_for_new_data() -> None:
    signature = data_signature()
    if signature <= state["loaded_signature"] or time.time() - signature / 1e9 < SETTLE_SECONDS:
        return
    if state.get("uploaded"):
        state["live_text"] = "● Шинэ өгөгдөл ирсэн — «Эхний snapshot сэргээх» дарж ачаална"
        update_freshness()
        return
    try:
        reload_data()
    except Exception as exc:  # an ETL run still writing, or a failed run; try again next minute
        state["live_text"] = f"● Шинэчлэл хойшлов ({type(exc).__name__}) — дараагийн шалгалтаар дахин оролдоно"
        update_freshness()
        return
    state["loaded_signature"] = signature
    state["live_text"] = _live_text("Шинэчлэгдлээ")
    update_freshness()
    message.text = f"<span style='color:#0F766E;font-weight:700;'>Өгөгдөл автоматаар шинэчлэгдлээ</span> ({datetime.now():%H:%M})"


state["loaded_signature"] = data_signature()
state["live_text"] = _live_text("Ачаалсан")
update_branch_chart()
update_movement_options()
refresh_all()
curdoc().add_periodic_callback(check_for_new_data, LIVE_CHECK_SECONDS * 1000)

PAGE_BACKGROUND = GlobalInlineStyleSheet(css="""
body {
  background-color: #EEF2F5;
  background-image:
    radial-gradient(circle at 1px 1px, rgba(18, 38, 58, 0.07) 1px, transparent 0),
    linear-gradient(180deg, #F6F8FA 0%, #E9EEF2 100%);
  background-size: 22px 22px, 100% 100%;
  background-attachment: fixed;
}
""")
curdoc().add_root(column(header, freshness_banner, controls, filters, tabs, sizing_mode="stretch_width",
                         stylesheets=[PAGE_BACKGROUND], styles={"padding": "8px 14px"}))
curdoc().title = "ROP Advanced Dashboard"
