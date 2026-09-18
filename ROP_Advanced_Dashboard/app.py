from __future__ import annotations

import base64
import json
from datetime import date

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
    NumeralTickFormatter,
    Select,
    Slider,
    StringFormatter,
    TableColumn,
    TabPanel,
    Tabs,
    TextInput,
)
from bokeh.palettes import Viridis256
from bokeh.plotting import figure
from bokeh.transform import cumsum

from common import (
    STATUS_ORDER,
    VED_ORDER,
    apply_missing_as_zero,
    build_status_snapshot,
    map_inventory,
    parse_inventory,
)
from metrics import MetricsCalculator
from queries import DashboardQuery, Filters
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
status_filter = MultiChoice(title="Status", value=[], options=STATUS_ORDER, width=260)
ved_filter = MultiChoice(title="VED", value=[], options=VED_ORDER, width=220)
category_filter = MultiChoice(title="Категори", value=[], options=options["categories"], width=260)
confidence_filter = MultiChoice(title="ROP confidence", value=[], options=options["confidences"], width=200)
branch_filter = Select(title="Салбар", value="ALL", options=["ALL"] + options["branches"], width=300)
search_input = TextInput(title="Барааны нэр хайх", placeholder="жишээ: Тобрекс", width=320)
min_gap_slider = Slider(title="Доод ROP gap", start=0, end=max(1000, db.max_gap()), value=0, step=1, width=240)

kpi_container = Div(text="", sizing_mode="stretch_width", height=135)
status_source = ColumnDataSource(data=dict(Status=[], Count=[], Angle=[], Color=[]))
ved_source = ColumnDataSource(data=dict(VED=[], Critical=[], Gap=[], Color=[]))
top_source = ColumnDataSource(data=dict(Name=[], Label=[], Branch=[], Gap=[], WeightedGap=[], VED=[], Status=[], OnHand=[], ROP=[], Color=[]))
scatter_source = ColumnDataSource(data=dict(Name=[], ROPPlot=[], InvPlot=[], ROP=[], Inventory=[], Gap=[], VED=[], Status=[], Color=[]))
hex_source = ColumnDataSource(data=dict(q=[], r=[], counts=[]))
branch_source = ColumnDataSource(data=dict(Branch=[], ROP=[], VROP=[], EROP=[], DROP=[]))
coverage_source = ColumnDataSource(data=dict(
    Branch=[], Category=[], Coverage=[], SKUCount=[], CoveredSKU=[], ROP=[], Inventory=[], Gap=[],
))
quality_source = ColumnDataSource(data=dict(Metric=[], Value=[], Label=[], Color=[]))
table_source = ColumnDataSource(data=dict())
review_source = ColumnDataSource(data=dict())
trend_source = ColumnDataSource(data={"DateLabel": [], "Risk": [], "Critical": [], "Gap": [], "Total": [], **{s: [] for s in STATUS_ORDER}})
movement_source = ColumnDataSource(data=dict())


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
<li>Дэлгэрэнгүй ROP байхгүй SKU-д fallback ROP хадгалсан бөгөөд confidence = “Бага”.</li>
</ul>
<h3 style="color:#12263A;">Өгөгдлийн загвар (star schema)</h3>
<p>Өгөгдөл <code>data/*.parquet</code> файлд хадгалагдаж, DuckDB-ээр SQL-ээр шүүгдэнэ.
Fact хүснэгтүүд (<code>fact_sku_status</code>, <code>fact_status_history</code>, <code>fact_branch_rop</code>) зөвхөн түлхүүр ба хэмжигдэхүүн агуулна;
барааны нэр, категори, салбарын нэр зэрэг тайлбар мэдээлэл <code>dim_sku</code>, <code>dim_branch</code>, <code>dim_date</code> хүснэгтээс холбогдоно.
Категори нь <code>SKU category.csv</code> файлаас авна; тохирохгүй SKU-г <code>data/category_review.csv</code>-д жагсаана.
Snapshot бүр <code>fact_status_history</code>-д огноогоор (DateKey) хадгалагдаж, «Тренд» табад харагдана.</p>
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
        confidences=tuple(confidence_filter.value),
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
        '</div>'
    )


def update_status_chart(f: Filters) -> None:
    counts = db.status_counts(f)
    total = max(int(counts.sum()), 1)
    status_source.data = {
        "Status": list(counts.index),
        "Count": counts.astype(int).tolist(),
        "Angle": (counts / total * 2 * np.pi).tolist(),
        "Color": [STATUS_COLORS[s] for s in counts.index],
    }


def update_ved_chart(f: Filters) -> None:
    ved = db.ved_summary(f)
    ved_source.data = {
        "VED": ved["VED"].tolist(),
        "Critical": ved["Critical"].astype(int).tolist(),
        "Gap": ved["Gap"].astype(float).tolist(),
        "Color": [VED_COLORS[v] for v in ved["VED"]],
    }


def update_top_chart(f: Filters) -> None:
    risk = db.top_shortages(f)
    names = [str(x)[:55] for x in risk["SKU_Name"]]
    branches = risk["BranchName"].fillna("").astype(str).tolist()
    labels = MetricsCalculator.unique_bar_labels(names, branches)
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


def update_scatter(f: Filters) -> None:
    # Density layer: every plotted SKU binned into hexagons.
    density = db.density_points(f)
    q, r, counts = MetricsCalculator.hexbin_log(density["ROP"], density["InventoryPosition"], HEX_SIZE)
    hex_source.data = {"q": q, "r": r, "counts": counts}
    hex_color_mapper.high = max(int(max(counts)) if counts else 1, 1)

    # Point layer: only stocked-out / below-ROP SKUs, capped at 3000 by priority.
    risk = db.risk_points(f)
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
    group = db.branch_exposure()
    labels = group["Branch"].astype(str).tolist()
    branch_source.data = {col: group[col].tolist() for col in ["Branch", "ROP", "VROP", "EROP", "DROP"]}
    branch_plot.y_range.factors = labels[::-1]


def update_coverage(f: Filters) -> None:
    grouped = MetricsCalculator.finalize_coverage(db.coverage(f))
    if grouped.empty:
        coverage_source.data = {key: [] for key in coverage_source.data}
        coverage_plot.x_range.factors = []
        coverage_plot.y_range.factors = []
        return
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
    order = db.filter_options()["categories"]
    present = set(grouped["Category"])
    coverage_plot.x_range.factors = [c for c in order if c in present] + sorted(present - set(order))
    coverage_plot.y_range.factors = grouped.groupby("BranchName")["ROP"].sum().sort_values(ascending=True).index.tolist()


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


def update_table(f: Filters) -> None:
    table_source.data = ColumnDataSource.from_df(db.table(f))


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
    update_scatter(f)
    update_table(f)
    update_coverage(f)
    update_quality()
    update_review_table()
    update_freshness()
    update_trend(f)


def reset_branch_filter() -> None:
    branch_filter.options = ["ALL"] + db.filter_options()["branches"]
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
        save_snapshot_files(mapped, state["status_wide"], review, snapshot_picker.value)
        save_upload_to_history(str(snapshot_picker.value))
        update_trend(current_filters())
        message.text = f"<span style='color:#0F766E;font-weight:700;'>Амжилттай:</span> {upload.filename} | {stats['matched_rows']:,}/{stats['inventory_rows']:,} мөр таарсан ({stats['mapping_rate_rows']:.1%}) | Scope: {stats['scope']}"
    except Exception as exc:
        message.text = f"<span style='color:#B91C1C;font-weight:700;'>Алдаа:</span> {friendly_error(exc)}"


def reset_callback() -> None:
    db.replace_table("fact_sku_status", initial_status)
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
for widget in [status_filter, ved_filter, category_filter, confidence_filter, branch_filter, search_input, min_gap_slider]:
    widget.on_change("value", filter_callback)

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
movement_download.js_on_click(CustomJS(args=dict(source=movement_source, filename="rop_status_movement.csv"), code=CSV_EXPORT_JS))

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
tabs = Tabs(tabs=[executive_panel, analytics_panel, branch_panel, coverage_panel, trend_panel, quality_panel, method_panel], sizing_mode="stretch_width")

update_branch_chart()
update_movement_options()
refresh_all()

curdoc().add_root(column(header, freshness_banner, controls, filters, tabs, sizing_mode="stretch_width"))
curdoc().title = "ROP Advanced Dashboard"
