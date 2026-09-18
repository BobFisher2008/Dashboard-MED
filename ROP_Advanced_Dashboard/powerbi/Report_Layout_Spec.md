# Power BI report layout

## Page 1 — Удирдлагын тойм

Top KPI cards:

- Total SKU
- Inventory Position
- Total ROP
- ROP Gap
- Critical V/E SKU
- Mapping Rate %

Visuals:

1. Donut: `DimStatus[Status]` by distinct SKU count.
2. Clustered column: VED by Stockout SKU and Below ROP SKU.
3. Horizontal bar: Top 20 `DimSKU[SKU_Name]` by `Weighted ROP Gap`.
4. Scatter: X = ROP, Y = Inventory Position, size = AvgMonthlyDemand, legend = Status.
5. Slicers: SnapshotDate, VED, Category, Status, ROP Confidence.

## Page 2 — VED ба эрсдэлийн хяналт

- Matrix: VED × Status with distinct SKU count and ROP Gap.
- Table: V/E risk SKU with On Hand, ROP, Gap, Coverage %, Supplier, ROP Confidence.
- Decomposition tree: Weighted ROP Gap → VED → Category → Supplier → SKU.
- Tooltip page: SKU metadata, lead time, ROP source, current inventory and branch ROP exposure.

## Page 3 — Салбарын ROP exposure

- Stacked bar: Branch ROP split by V/E/D.
- Map or matrix by branch group if geographic master data becomes available.
- Table: Branch, Active Branch SKU, Branch V ROP, Branch E ROP, Branch D ROP.
- Warning banner: without branch inventory this page shows exposure, not stock status.

## Page 4 — SKU drill-through

Drill-through key: `SKU_ID`.

- KPI: On Hand, ROP, Gap, Coverage %, Avg Monthly Demand, Lead Time.
- Branch ROP distribution by branch.
- Inventory source lines: Manufacturer, Supplier, MatchStatus.
- ROP methodology fields: ROP source, confidence, VED, category.

## Page 5 — Өгөгдлийн чанар

- Mapping Rate %, unmatched rows, unmatched quantity.
- Missing inventory SKU, missing ROP SKU, low-confidence ROP SKU.
- Table from `MappingReview` with suggested SKU and similarity.
- Table of negative inventory, unknown branches and zero/unknown lead time.

## Conditional formatting

- Stockout: dark red.
- Below ROP: orange.
- Missing data: purple.
- Missing ROP: gray.
- Normal: teal.
- Excess: blue.
- V: dark red, E: amber, D: teal.
