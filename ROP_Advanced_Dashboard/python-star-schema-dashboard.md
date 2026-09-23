# Python Interactive Dashboard: Star Schema Architecture Guide

## Overview
This guide covers best practices for building interactive dashboards in Python using a star schema data model. Star schemas are ideal for analytics because they optimize query performance and simplify business logic representation.

## Star Schema Fundamentals

### Core Components
- **Fact Table**: Central table containing measurements/metrics and foreign keys to dimensions
  - Contains transactional data and quantitative values
  - Typically large, optimized for reads
  - Grain: defines the level of detail (e.g., per order, per day, per customer)

- **Dimension Tables**: Descriptive attributes about business entities
  - Products, Customers, Dates, Locations, etc.
  - Relatively small and denormalized
  - Enable filtering and grouping in dashboards
  - Contain descriptive text and hierarchies

### Design Principles
- Keep fact tables normalized (only keys + measures)
- Denormalize dimensions for query simplicity
- Use surrogate keys for dimension tables
- Maintain slowly changing dimensions (SCD Type 1, 2, or 3)
- Ensure conformed dimensions across fact tables

## Python Technology Stack

### Data Processing & Querying
```python
# Primary options for data access
pandas          # DataFrames, in-memory operations
sqlalchemy      # ORM and raw SQL queries
polars          # Fast DataFrames (for large datasets)
duckdb          # In-process SQL queries (fast)
```

### Dashboard Frameworks
```python
# Interactive dashboards
streamlit       # Easiest for rapid development, reactive
plotly          # Rich interactive visualizations
dash            # Production-grade interactive apps
bokeh           # Complex interactive visualizations
altair          # Declarative visualization grammar
```

### Database Backends
```python
# Star schema storage
postgresql      # Open-source, reliable
snowflake       # Cloud data warehouse (recommended)
bigquery        # Google Cloud analytics
redshift        # AWS data warehouse
duckdb          # Embedded SQL (for smaller projects)
```

## Recommended Architecture

### Layered Approach
```
Data Warehouse (Star Schema)
        ↓
ETL/Data Pipeline (Python scripts or dbt)
        ↓
Analytics Layer (Computed views/aggregations)
        ↓
Dashboard Application (Streamlit/Dash)
        ↓
Frontend Caching & State Management
```

### Typical Stack Example
```
┌─────────────────────────────────────┐
│  Streamlit/Dash Application         │
│  (Interactive UI, Filters, Charts)  │
└────────────────┬────────────────────┘
                 ↓
        ┌────────────────┐
        │ Pandas/Polars  │
        │ (In-memory)    │
        └────────────────┘
                 ↓
┌─────────────────────────────────────┐
│  SQL Queries (SQLAlchemy/Raw SQL)   │
└────────────────┬────────────────────┘
                 ↓
┌─────────────────────────────────────┐
│  Database (Snowflake/PostgreSQL)    │
│  ├── fact_orders                    │
│  ├── fact_web_events                │
│  ├── dim_customers                  │
│  ├── dim_products                   │
│  ├── dim_dates                      │
│  └── dim_locations                  │
└─────────────────────────────────────┘
```

## Implementation Patterns

### 1. Query Builder Pattern
```python
from sqlalchemy import create_engine, select
from datetime import datetime, timedelta

class DashboardQuery:
    def __init__(self, connection_string):
        self.engine = create_engine(connection_string)
    
    def get_sales_by_date(self, start_date, end_date):
        query = """
        SELECT 
            dd.date_key,
            dd.date,
            SUM(f.revenue) as total_revenue,
            COUNT(DISTINCT f.order_id) as order_count
        FROM fact_orders f
        JOIN dim_dates dd ON f.date_key = dd.date_key
        WHERE dd.date BETWEEN %s AND %s
        GROUP BY dd.date_key, dd.date
        ORDER BY dd.date DESC
        """
        return pd.read_sql(query, self.engine, params=[start_date, end_date])
```

### 2. Caching Strategy
```python
import streamlit as st
from datetime import datetime, timedelta

@st.cache_data(ttl=3600)  # Cache for 1 hour
def get_dashboard_data(start_date, end_date):
    """Fetch and cache data from warehouse"""
    query = DashboardQuery(connection_string)
    return query.get_sales_by_date(start_date, end_date)

# Only re-runs query if parameters change or cache expires
data = get_dashboard_data(start, end)
```

### 3. Dimension Filtering Pattern
```python
def build_filters(df_dimensions, fact_df):
    """Build dimension-based filters for interactivity"""
    
    col1, col2, col3 = st.columns(3)
    
    with col1:
        selected_products = st.multiselect(
            "Products",
            options=df_dimensions['dim_products']['product_name'].unique()
        )
    
    with col2:
        selected_regions = st.multiselect(
            "Regions",
            options=df_dimensions['dim_locations']['region'].unique()
        )
    
    with col3:
        date_range = st.date_input("Date Range", value=[start_date, end_date])
    
    # Apply filters to fact data
    filtered = fact_df[
        (fact_df['product_id'].isin(selected_products)) &
        (fact_df['region_id'].isin(selected_regions)) &
        (fact_df['date'] >= date_range[0]) &
        (fact_df['date'] <= date_range[1])
    ]
    
    return filtered
```

### 4. Aggregation & Metrics
```python
class MetricsCalculator:
    """Pre-calculate common metrics for performance"""
    
    @staticmethod
    def calculate_kpis(fact_df):
        return {
            'total_revenue': fact_df['revenue'].sum(),
            'avg_order_value': fact_df['revenue'].mean(),
            'order_count': fact_df['order_id'].nunique(),
            'customer_count': fact_df['customer_id'].nunique(),
            'conversion_rate': (fact_df[fact_df['converted']].shape[0] / 
                              len(fact_df) * 100)
        }
    
    @staticmethod
    def get_trend_data(fact_df, group_by_col, metric_col):
        """Generate trend data for time series charts"""
        return fact_df.groupby(group_by_col)[metric_col].sum().reset_index()
```

## Database Query Optimization

### Star Schema Advantages
- **Fact tables are pre-joined**: Just one fact table + dimensional lookups
- **Pre-aggregation**: Consider materialized views for common aggregations
- **Indexing strategy**:
  ```sql
  -- Fact table indexes
  CREATE INDEX idx_fact_orders_date ON fact_orders(date_key);
  CREATE INDEX idx_fact_orders_customer ON fact_orders(customer_key);
  
  -- Dimension table indexes
  CREATE INDEX idx_dim_products_key ON dim_products(product_key);
  CREATE INDEX idx_dim_customers_key ON dim_customers(customer_key);
  ```

### Query Example
```python
# Optimized star schema query
query = """
SELECT 
    dd.year,
    dd.quarter,
    dp.category,
    dl.country,
    SUM(f.revenue) as total_revenue,
    SUM(f.quantity) as total_quantity,
    COUNT(DISTINCT f.order_id) as order_count
FROM fact_orders f
JOIN dim_dates dd ON f.date_key = dd.date_key
JOIN dim_products dp ON f.product_key = dp.product_key
JOIN dim_locations dl ON f.location_key = dl.location_key
JOIN dim_customers dc ON f.customer_key = dc.customer_key
WHERE dd.year = 2024
GROUP BY dd.year, dd.quarter, dp.category, dl.country
HAVING COUNT(DISTINCT f.order_id) > 0
"""
```

## Dashboard Example: Streamlit Implementation

```python
import streamlit as st
import pandas as pd
import plotly.express as px
from dashboard_query import DashboardQuery

st.set_page_config(page_title="Sales Dashboard", layout="wide")
st.title("📊 Sales Analytics Dashboard")

# Initialize query builder
db = DashboardQuery(st.secrets["database_url"])

# Sidebar filters
st.sidebar.header("Filters")
date_range = st.sidebar.date_input("Select Date Range", value=[start, end])
selected_category = st.sidebar.multiselect("Product Category", 
                                          options=db.get_categories())

# Fetch data (cached)
@st.cache_data(ttl=3600)
def load_data():
    return db.get_sales_by_date(date_range[0], date_range[1])

df = load_data()
df = df[df['category'].isin(selected_category)] if selected_category else df

# KPIs Row
col1, col2, col3, col4 = st.columns(4)
col1.metric("Total Revenue", f"${df['revenue'].sum():,.0f}")
col2.metric("Order Count", f"{df['order_id'].nunique():,}")
col3.metric("Avg Order Value", f"${df['revenue'].mean():,.0f}")
col4.metric("Customers", f"{df['customer_id'].nunique():,}")

# Charts
col1, col2 = st.columns(2)

with col1:
    revenue_trend = df.groupby('date')['revenue'].sum().reset_index()
    fig = px.line(revenue_trend, x='date', y='revenue', title='Revenue Trend')
    st.plotly_chart(fig, use_container_width=True)

with col2:
    category_sales = df.groupby('category')['revenue'].sum().reset_index()
    fig = px.bar(category_sales, x='category', y='revenue', title='Sales by Category')
    st.plotly_chart(fig, use_container_width=True)

# Data table
st.subheader("Orders Detail")
st.dataframe(df.sort_values('date', ascending=False).head(100))
```

## Performance Optimization Tips

1. **Aggregate at warehouse level**: Pre-compute daily aggregations
2. **Use materialized views**: Cache expensive calculations
3. **Implement pagination**: Don't load all data client-side
4. **Columnar storage**: Use Parquet or Snowflake for efficient I/O
5. **Query parallelization**: Use async queries for independent requests
6. **Data reduction**: Filter at source (SQL) before Python processing

## Common Pitfalls to Avoid

- ❌ Pulling entire fact tables into memory
- ❌ Complex JOINs in dashboard queries (pre-aggregate in warehouse)
- ❌ No caching layer (re-running expensive queries)
- ❌ Unindexed fact table keys
- ❌ Mixing OLTP database with analytics (use dedicated warehouse)
- ✅ Keep dimension tables small and denormalized
- ✅ Use conformed dimensions across multiple fact tables
- ✅ Implement slowly changing dimensions properly
- ✅ Cache frequently accessed data
- ✅ Filter early in SQL, not in Python

## Testing & Validation

```python
import pytest

def test_star_schema_integrity():
    """Verify referential integrity"""
    db = DashboardQuery(test_connection)
    
    # Check all fact keys reference valid dimensions
    fact_df = db.get_all_facts()
    dim_products = db.get_dimension('products')
    
    orphaned_keys = fact_df[~fact_df['product_key'].isin(dim_products['product_key'])]
    assert len(orphaned_keys) == 0, "Orphaned product keys found"

def test_metric_calculations():
    """Verify KPI calculations"""
    test_data = load_test_data()
    kpis = MetricsCalculator.calculate_kpis(test_data)
    
    assert kpis['total_revenue'] == 10000
    assert kpis['order_count'] == 50
```

## Deployment Considerations

- Use environment variables for database credentials
- Implement authentication/authorization (Streamlit Cloud, custom SSO)
- Monitor query performance and add indexes as needed
- Set up data refresh schedules (incremental loads)
- Document dimension table update frequency
- Implement error handling and data quality checks
- Use version control for SQL queries and Python code

## Resources & Further Reading

- Star Schema Design: Kimball & Ross "The Data Warehouse Toolkit"
- Streamlit Docs: https://docs.streamlit.io
- SQLAlchemy: https://docs.sqlalchemy.org
- Snowflake Python: https://docs.snowflake.com/developer-guide/python
- Plotly: https://plotly.com/python

---

**Last Updated**: 2024  
**Version**: 1.0

---

# Implementation Status — ROP Advanced Dashboard

This section records how the guide above has been applied to this project. Update it whenever the project changes.

**Last updated**: 2026-09-21

## Decisions
| Topic | Guide suggests | Chosen for this project | Why |
|---|---|---|---|
| Dashboard framework | Streamlit / Dash | **Bokeh server** (kept), refactored into layers | Existing 6-tab app worked; lower risk than a rewrite |
| Warehouse | Snowflake / PostgreSQL / DuckDB | **DuckDB + Parquet** (`data/*.parquet`) | Embedded, no server, fast enough for ~1.1M history rows |
| Category source | — | **`SKU category.csv`** overrides `dim_sku.Category` | Business-owned category list |
| VED source | — | **`VED ангилал.csv`** overrides the VED of every SKU it lists, in `dim_sku` and in every fact | Business-owned V/E/D list; the workbook's VED stays in `fact_branch_rop.VED_ROP`, so ROP and Z_VED remain explainable |
| ABC / XYZ | — | **ABC / XYZ** tab over a separate view `v_abc_xyz` | The classes live in `fact_branch_rop` (per SKU × branch); joining them into `v_status` would double the cost of every other query |
| History | Time dimension (`dim_date`) | **`fact_status_history`** keyed by `DateKey` + Trend tab | Snapshots existed but were never used |
| Orchestration | cron / Airflow | **Dagster** (`dagster_rop/`) over the existing scripts | Asset lineage, run history, retries and typed checks without rewriting the ETL |
| Unified database | one warehouse | **`data/warehouse.duckdb`** stays the single database, now owned by the `warehouse_db` asset | Zero infrastructure; `app.py`, DBeaver and Power BI ODBC already read it |

## Architecture (as built)
```
Source files (Excel / CSV, snapshots/status_*.csv.gz)
        ↓  Dagster assets (dagster_rop/) — or the same scripts by hand
           build_model.py · update_rop.py · refresh_inventory.py · warehouse.py build
data/*.parquet  (+ data/warehouse.duckdb — the unified database)
        ↓  warehouse.connect()  — in-memory DuckDB, tables loaded at app start
queries.py      DashboardQuery + Filters  — SQL star joins, filters in SQL, cached per data version
        ↓
metrics.py      MetricsCalculator  — pure calculations (KPIs, coverage, ABC / XYZ rollup, heatmap shares, hexbin, trend table)
        ↓
app.py          Bokeh UI  — widgets, figures, callbacks only
```

## Star schema
| Table | Grain / role | Rows (2026-09-21) |
|---|---|---|
| `dim_sku` | SKU master; Category from `SKU category.csv`; VED from `VED ангилал.csv` where listed (`VED_Source`) | 6,781 |
| `dim_branch` | Branch; flags `IsNetwork`, `IsMapped` (UNMAPPED::*), `IsExcluded` | 140 |
| `dim_date` | Day; `DateKey` yyyymmdd, `HasSnapshot` | 1,095 |
| `dim_status`, `dim_ved`, `dim_category` | Lookup / ordering (`Severity` used for movement) | 6 / 4 / 6 |
| `fact_branch_rop` | SKU × branch ROP parameters, ABC / XYZ; `VED_ROP` = the VED the workbook calculated ROP with | 193,469 |
| `fact_sku_status` | SKU × branch × date — current snapshot | 265,695 |
| `fact_status_history` | SKU × branch × date — all snapshots (7 dates) | 1,413,655 |
| `fact_inventory_snapshot` | Inventory line | 179,456 |
| `alias_mapping`, `mapping_review`, `data_dictionary` | Mapping support and column docs | 6,768 / 13,144 / 14 |

`data/warehouse.duckdb` holds all 13 tables (2.08M rows, 40 MB).

Fact tables hold keys, measures and degenerate attributes only (Status, VED, Supplier…); names, categories and branch names come from dimensions via `v_status` / `v_history` views. `v_abc_xyz` adds each status row's branch ABC / XYZ class (from `fact_branch_rop`; "Тодорхойгүй" when the row has no branch ROP row) and is used by the ABC / XYZ tab only.

## Guide checklist
- [x] Fact tables slimmed to keys + measures; dimensions denormalized
- [x] Query builder pattern — `queries.DashboardQuery`
- [x] Dimension filtering in SQL, parameterized — `queries.Filters.where()`
- [x] Caching — per `(method, filters, data version)`; cleared on upload / threshold change / reset
- [x] Metrics class — `metrics.MetricsCalculator`
- [x] Columnar storage — Parquet
- [x] Referential integrity tests — `tests/test_star_schema.py`, `warehouse.integrity_checks()`
- [x] Metric calculation tests — `tests/test_metrics.py`
- [x] Watcher tests — `tests/test_watch_inventory.py`
- [x] Orchestration tests — `tests/test_dagster_pipeline.py`
- [x] VED file and ABC / XYZ tests — `tests/test_classification.py` (116 tests total, all passing)
- [x] Time dimension used — Trend tab + snapshot comparison
- [ ] Slowly changing dimensions (history uses current `dim_sku`, e.g. today's Category and VED, and today's ABC / XYZ, for past dates) — SCD Type 1 for now
- [x] ETL orchestration — Dagster asset graph with lineage, run history, retries and a run queue of 1 (`dagster_rop/`)
- [x] Data-quality checks as first-class assets — 5 asset checks on `warehouse_db`
- [ ] Authentication / deployment beyond local `bokeh serve` and local `dagster dev`
- [x] Automatic refresh — the Dagster `inbox_sensor` watches `inbox/` and launches a refresh per new file (see *ETL orchestration*)
- [ ] `watch_inventory.py` still works and is kept as the no-Dagster fallback, but is no longer the production path
- [ ] Manual `schedule_refresh.ps1` still exists (unchanged; not re-tested) — superseded twice over

## Commands
| Task | Command |
|---|---|
| Install dependencies | `pip install -r requirements.txt` |
| Rebuild warehouse (categories, VED, history, checks) | `python warehouse.py build --categories "SKU category.csv" --ved "VED ангилал.csv"` |
| Rebuild history only | `python warehouse.py history` |
| Integrity checks | `python warehouse.py check` or `python qa_check.py` |
| New inventory file | `python refresh_inventory.py --inventory <file> --snapshot-date YYYY-MM-DD` |
| New ROP workbook | `python update_rop.py --rop "ROP final.xlsb"` |
| Power BI CSV export | add `--powerbi` to any of the above |
| Tests | `python -m pytest tests -q` |
| Run dashboard | `run_dashboard.bat` (`python -m bokeh serve --show app.py`) |

### Dagster (orchestration)
| Task | Command |
|---|---|
| Start the UI + daemon | `run_dagster.bat` → http://localhost:3000 |
| Refresh from the newest file in `inbox/` | `dagster job execute -m dagster_rop.definitions -j inventory_refresh_job` |
| Reload the ROP workbook | `dagster job execute -m dagster_rop.definitions -j rop_reload_job` |
| Full rebuild | `dagster job execute -m dagster_rop.definitions -j full_rebuild_job` |
| Checks only (read-only) | `dagster job execute -m dagster_rop.definitions -j warehouse_check_job` |
| Enable / disable the inbox sensor | `dagster sensor start` / `stop` `-m dagster_rop.definitions inbox_sensor` |
| Validate the code location | `dagster definitions validate` |
| Refresh a specific file | add `--config-json '{"ops":{"inventory_snapshot":{"config":{"inventory_file":"C:/path/x.csv","snapshot_date":"2026-09-21"}}}}'` |

`DAGSTER_HOME` must point at `dagster_home/`; `run_dagster.bat` sets it, and the CLI needs it exported for run history to persist. Overrides via environment: `DAGSTER_ROP_PROJECT_DIR`, `DAGSTER_ROP_POWERBI`, `DAGSTER_ROP_MISSING_AS_ZERO`, `DAGSTER_ROP_EXCESS_THRESHOLD`.

### File watcher (fallback, superseded by the sensor)
| Task | Command |
|---|---|
| Watch inbox (foreground) | `watch_inventory.bat` or `python watch_inventory.py` |
| Process inbox once, then exit | `python watch_inventory.py --once` |
| Run watcher at logon (background) | `.\install_inventory_watcher.ps1` · `-Status` · `-Uninstall` |

Do not run the watcher and the Dagster sensor at the same time — both would claim the same file.

## ETL orchestration (Dagster)

`dagster_rop/` wraps the existing scripts in a Dagster asset graph. It does not
replace the ETL — every asset calls the same function the CLI calls, so
`python update_rop.py` and a Dagster run do exactly the same thing. What
Dagster adds is lineage, run history, retries, typed data-quality checks and a
UI.

```
rop_workbook (ROP final.xlsb, hashed)  ──►  branch_rop ──┐
                                                         ├─►  inventory_snapshot
inventory_export (newest file in inbox/, hashed)  ───────┘            │
                                                                      ▼
                                                               status_history
                                                                      │
sku_category_csv (SKU category.csv, hashed)  ─────────────────────────┤
ved_classification_csv (VED ангилал.csv, hashed)  ────────────────────┤
                                                                      ▼
                                                               warehouse_db  ──►  5 asset checks
                                                               (data/warehouse.duckdb)
```

| Asset | Calls | Writes |
|---|---|---|
| `rop_workbook`, `sku_category_csv`, `ved_classification_csv`, `inventory_export` | SHA-256 of the file | nothing — observed sources, so an asset only re-runs when the file really changed |
| `branch_rop` | `update_rop.load_rop()` | `dim_sku`, `dim_branch`, `fact_branch_rop` (VED from the VED file where `dim_sku.VED_Source` says so) |
| `inventory_snapshot` | `refresh_inventory.refresh()` | `fact_sku_status`, `fact_inventory_snapshot`, `mapping_review`, a new `snapshots/` pair |
| `status_history` | `warehouse.rebuild_history()` | `fact_status_history`, `dim_date` |
| `warehouse_db` | `warehouse.build()` with both files | every table + `data/warehouse.duckdb` — **the unified database** — plus `data/category_review.csv`, `data/ved_review.csv` |

The assets are "files in place": they rewrite `data/*.parquet` rather than
handing DataFrames to an IO manager, because `app.py`, the tests, DBeaver and
Power BI already read those files. Nothing about the dashboard changed.

### The unified database
`data/warehouse.duckdb` is the single database and is rebuilt by the
`warehouse_db` asset. It holds all 13 tables (1.45M rows, 40 MB) and is what
external tools should connect to. The `DuckDBWarehouse` resource opens it
**read-only** so a query can never lock out or corrupt a running rebuild; the
write path stays `warehouse.build_duckdb_file()`, which writes a temp file and
renames it into place. `app.py` still uses `warehouse.connect()` over the
Parquet files, so the ETL can rewrite the database while the dashboard is up.

### Jobs, sensor and schedule
| Name | What it does |
|---|---|
| `inventory_refresh_job` | `inventory_snapshot` → `status_history` → `warehouse_db` + checks. Skips `branch_rop`, which would re-read the 14 MB workbook for nothing. 2 retries, 30 s apart. |
| `rop_reload_job` | `branch_rop` → `status_history` → `warehouse_db` + checks |
| `full_rebuild_job` | everything, in dependency order (~81 s on real data) |
| `warehouse_check_job` | the 5 checks only, read-only (~1.5 s) |
| `inbox_sensor` | polls `inbox/` every 30 s and launches `inventory_refresh_job` per new export. Stopped by default. |
| `daily_warehouse_check` | `warehouse_check_job`, weekdays 07:00 Asia/Ulaanbaatar. Stopped by default. |

### Asset checks on `warehouse_db`
| Check | Severity | Fails when |
|---|---|---|
| `referential_integrity` | ERROR | a fact key has no dimension row, or a fact's grain is not unique |
| `measure_ranges` | ERROR | negative ROP gap, coverage outside 0..1, rows on excluded branches, a VED that is not V/E/D/Тодорхойгүй or differs from the VED file (`*_invalid_ved`) |
| `row_counts` | ERROR | a dimension or fact table came out empty |
| `mapping_rate` | WARN | fewer than 90% of inventory rows matched a SKU |
| `snapshot_freshness` | WARN | the newest snapshot is more than 10 days old |

The first three come from `warehouse.integrity_checks()`, run once and split
into the three groups a reader would act on differently. `negative_rop_rows`
and `*_excluded_branch_rows` end in `_rows` but must be **zero**, so they
belong to `measure_ranges`, not to `row_counts`.

### How the sensor replaces `watch_inventory.py`
| Watcher mechanism | Dagster equivalent |
|---|---|
| block until size / mtime stop changing (`--settle`) | the cursor stores (size, mtime); a run is launched on the next tick only if they are unchanged |
| SHA-256 in `inbox/.watcher_state.json` | the digest is the `run_key`, which Dagster itself refuses to run twice |
| single-file `queue.Queue` + `logs/inventory_watcher.lock` | run queue with `max_concurrent_runs: 1` in `dagster_home/dagster.yaml` |
| `<file>.log` next to the failed file | the run's own logs and metadata in the UI |
| `--on-success` / `--on-failure` hooks | run-status sensors (not yet written) |
| move to `processed/` or `failed/` | still done, by the `inventory_snapshot` asset itself |

File filtering and snapshot-date parsing are **imported** from
`watch_inventory` rather than reimplemented, so both paths agree on which files
count and what date a name means.

The cursor must be written on skip ticks too. Returning a bare `SkipReason`
discards it, and the file then reports "still settling" for ever — caught by a
live daemon run, now covered by a test.

### Verification (2026-09-21)
1. `python -m pytest tests -q` — 92 passed (31 new in `tests/test_dagster_pipeline.py`).
2. `warehouse_check_job` against the live warehouse: all 5 checks passed in 1.5 s.
3. `full_rebuild_job` on a copy of the project with a 179,456-row export:
   `branch_rop` 26.7 s → `inventory_snapshot` 18.3 s → `status_history` 16.3 s →
   `warehouse_db` 19.7 s, all 5 checks passed. Snapshot date `2026-09-21` was
   read from the file name, the file was moved to `inbox/processed/`, and the
   rebuilt `warehouse.duckdb` held 13 tables / 1.45M rows.
4. Both CLIs still work after the refactor: `refresh_inventory.py --inventory … --snapshot-date …`
   and `update_rop.py --rop "ROP final.xlsb"`.
5. `dagster dev` with the sensor enabled: the daemon reports all 7 daemons,
   evaluates `inbox_sensor` every 30 s, and picks up a file dropped into `inbox/`.

### Notes
- Dagster 1.13 rejects a postponed (string) annotation on an asset's `context`
  parameter, so `dagster_rop/assets.py` and `checks.py` must **not** use
  `from __future__ import annotations`.
- `DAGSTER_HOME` must be set for the CLI as well as the UI, or run history goes
  to a temporary instance.
- Windows prints "Compute log capture is disabled" unless
  `PYTHONLEGACYWINDOWSSTDIO` is set; step logs still appear in the UI.

## Automatic inventory refresh
```
inventory export (xlsx / xlsb / csv) copied into inbox/
        ↓  watchdog on_created / on_moved event (non-recursive; ignores ~$ lock files, hidden files, SKU category.csv, ROP final.xlsb)
wait until size is stable for --settle seconds and the file is no longer locked
        ↓  SHA-256 already processed OK? → move to processed/, skip
snapshot date from the file name (20260919, 2026-09-19, 09192026, 19092026, 0919) → fallback: file modified date
        ↓
refresh_inventory.py --inventory <file> --snapshot-date <date>   (updates current fact + history + dims)
        ↓
warehouse.py check   (integrity checks)
        ↓
move to inbox/processed/ or inbox/failed/ with <file>.log · record in inbox/.watcher_state.json
        ↓
optional hook: --on-success / --on-failure "<command>"  (env: INVENTORY_FILE, SNAPSHOT_DATE, REFRESH_STATUS)
```
- Files already in `inbox/` at start-up are processed first; files are processed one at a time in arrival order.
- Only one watcher per project can run (OS file lock on `logs/inventory_watcher.lock`).
- Log: `logs/inventory_watcher.log` (rotating, 5 × 2 MB).
- The running dashboard picks up new data when the browser page is reloaded (each Bokeh session reads the warehouse at start).
- Options passed through to the refresh: `--excess-threshold`, `--missing-as-zero`, `--powerbi`. Use `--watch-dir .` to watch the project folder itself.
- Linux / macOS: the same script works (watchdog uses inotify / FSEvents); run it under systemd or launchd instead of Task Scheduler.

### Verification
1. `python -m pytest tests/test_watch_inventory.py -q`: date parsing, file filtering, copy-in-progress wait, the success / duplicate / failure pipeline against a stub project, and a live watchdog run.
2. Manual: start `watch_inventory.bat` and copy an export into `inbox/`. Then check:
   - the log shows `Detected …` → `Refreshing …` → `OK …`
   - the file is in `inbox/processed/`
   - `data/metadata.json` has the new `snapshot_date`
   - the Тренд tab shows the new date after a page reload
3. Copy the same file in again: the log reports it as a duplicate and it is skipped.
4. Background mode: `.\install_inventory_watcher.ps1`, then `.\install_inventory_watcher.ps1 -Status`.

## Repository
- Git root is the parent folder `ROP_Advanced_Dashboard_Package (1)`.
- `master`: baseline commit of the original package.
- `star-schema-refactor`: the refactor described below.
- `.gitignore` keeps these out of Git; they stay on disk only:
  - business data: `data/`, `snapshots/`, `powerbi_data/`, `inbox/`, `*.xlsb`, `*.xlsx`
  - generated files: `*.duckdb`, dashboard previews, `QA_REPORT.txt`
  - run state: `logs/`, `dagster_home/` (Dagster run history, event logs, pickled asset outputs)
  - backups and `__pycache__/`
- Layout: `dagster_rop/` (orchestration) · `tests/` · the ETL scripts and `app.py` at the root.
- A fresh clone needs the source files plus `python warehouse.py build` before the app can run.

## Change log
### 2026-09-23 — ROP workbook rows joined to SKUs by name; supervisor fix
- **Fixed: ROP values sat on the wrong product.** `update_rop.py` joined «ROP final.xlsb» rows to `dim_sku` by the workbook's «SKU ID», but the workbook numbers products its own way (they diverge from ID 2274 on, and 4 IDs are shared by two products). 66,387 of 199,749 rows (4,416 SKUs) landed on another product.
  - New `common.match_rop_skus`: rows are matched by «Нэр төрөл» like the category and VED files (exact name → master key → alias); each row goes to exactly one SKU, its own name first. The workbook ID is kept as `WorkbookSKU_ID`; rows with no match are dropped and counted.
  - Today's workbook: all 199,749 rows match by exact name; the name → SKU mapping is one-to-one. Stats are in `metadata.json` → `rop_sku_match`.
  - Verified: every non-excluded workbook row is in `fact_branch_rop` with its own product's ROP (21 duplicate SKU × branch rows resolve by the usual rule).
  - SKUs that an earlier load gave a detail ROP but the workbook no longer lists fall back to `ROP_Current` («Нэгтгэл дэх fallback ROP», confidence Бага) instead of keeping a stale value: 89 not in the workbook, 87 listed only at excluded branches. The 93 former fallback SKUs now have their detail ROP.
- **Effect on 2026-09-22** (before → after): ROP gap 36,917 → 24,288; «ROP байхгүй» 83,550 → 63,842; «Өгөгдөл алга» 120,471 → 100,763; «Илүүдэл» 41,522 → 58,525; «Хэвийн» 12,287 → 15,429; «ROP-оос доош» 7,940 → 7,499; «Тасарсан» 4 → 8; critical V/E 5,421 → 5,877; network ROP 440,583 → 337,728. Branch ROP total is unchanged (337,637): the same rows, on the right products.
- **Trend history has a break at 2026-09-22.** Snapshots up to 2026-09-21 were computed with the ID join and are not recomputed; the jump between 09-21 and 09-22 is the fix, not a change in stock.
- **A ROP reload now rewrites the current date's `snapshots/status_*.csv.gz`** (`warehouse.save_status_snapshot`). Before, the next history rebuild brought back that date's status under the previous ROP (this also affected the 2026-09-22 A × 90% reload).
- **Supervisor fix.** `dagster_supervisor.ps1` started Dagster in its own console, so the Ctrl+C that Dagster's processes get on shutdown also ended the supervisor (task result `0xC000013A`); after that nothing restarted Dagster. Dagster now gets a separate hidden console, and the «ROP Dagster» task re-launches the supervisor every 5 minutes if it is not running. Verified: killing Dagster left the supervisor running and it restarted Dagster after 60 s.
- Backup of `data/` from before the reload: `_backup_before_sku_name_match/` (with `before_metrics.json`).

### 2026-09-22 — Automatic refresh end to end
- **Dashboard live refresh** (`app.py`): every open page checks every 60 s whether `data/*.parquet`, `metadata.json` or «Салбарын жагсаалт.txt» changed, waits until the newest file is 20 s old (an ETL run writes several files), then reloads every table and redraws every tab. Filters stay; the header shows «● Шинэчлэгдлээ HH:MM». A page with an uploaded file is not overwritten; it shows «Шинэ өгөгдөл ирсэн» instead.
- **`source_files_sensor`** (Dagster, on by default): watches «ROP final.xlsb» and «ABC XYZ.xlsx» → `rop_reload_job`, and «SKU category.csv» / «VED ангилал.csv» → new `classification_reload_job` (`warehouse_db` only). A file counts once its size and mtime held for a whole 60 s tick, so a workbook Excel is still saving is never read. The first tick only records the files: starting Dagster does not reload.
- Together: save a file in Excel or drop an export in `inbox/` and every open dashboard updates by itself. Measured end to end on 2026-09-22 (ROP final.xlsb saved 17:09:46): sensor saw it +46 s, run launched +107 s, warehouse rewritten +223 s, open dashboard redrawn 17:14 - about 4½ minutes.

### 2026-09-21 — VED from «VED ангилал», new ABC / XYZ tab, ROP confidence filter removed
- **`VED ангилал.csv` now sets the VED of every SKU it lists** (columns: product name, product group, VED).
  - `common.load_ved_map()` matches names exactly like the category file: normalized key → master key, then alias. There's no fuzzy matching; the near misses are other pack sizes (XS vs XXS, 10 см vs 15 см, 100 мл vs 500 мл).
  - Seven products exist as two SKUs whose names normalize to the same key: `N` vs `№`, a hyphen vs a space, capitals (e.g. 1282 «Бумба N6», 2041 «Бумба №6»). A file row therefore matches both SKUs. Conflicts resolve in this order: the row with the SKU's own name, then master key over alias, then the most critical VED (V > E > D).
  - Cyrillic В / Е / Д count as V / E / D. Anything else is reported and not applied.
  - `apply_ved_map()` writes `dim_sku.VED` and `dim_sku.VED_Source` ("VED ангилал" / "ROP мастер"). SKUs the file does not list keep their VED.
  - `warehouse.apply_sku_ved()` carries the VED into `fact_branch_rop`, `fact_sku_status` and `fact_status_history`, and recomputes `WeightedGap`, `PriorityScore` and `IsCritical` on the rows that changed.
    - It runs in `build()`, `rebuild_history()` and `update_rop.load_rop()`: snapshot files and the workbook still carry the old VED and would otherwise bring it back.
    - Inventory refreshes and in-app uploads inherit it from `fact_branch_rop`.
  - **ROP and Z_VED are unchanged** — they are what `ROP final.xlsb` calculated. The workbook's VED is kept in the new column `fact_branch_rop.VED_ROP`.
  - `build --ved` writes `data/ved_review.csv`: unmatched names, invalid values, conflicts, SKUs not in the file, and SKUs whose VED now differs from the workbook.
  - New integrity checks `fact_*_invalid_ved` (Dagster `measure_ranges`): the VED must be a known class and must equal the file's for every listed SKU.
  - Dagster: new observed source `ved_classification_csv` → `warehouse_db`; `RopProject.ved_csv`. The watcher and the inbox sensor ignore `VED ангилал.csv`.
  - `build_status_snapshot` and `apply_sku_ved` share one formula, `common.ved_measures()`.
  - The category loader shares the name matching (`_match_source_names`); its `category_review.csv` now labels exact-name matches "Exact name". Categories are unchanged (checked against the backup).
- **Real data**:

  | | Value |
  |---|---|
  | File rows | 7,714 (V 796 / E 1,163 / D 5,755) |
  | SKUs covered | 5,828 of 6,781 |
  | Names without a SKU | 1,884 — 1,771 of them are products in the inventory export that have no SKU in the ROP master |
  | Conflicts | 3 (SKUs 1282, 2041, 3445) |
  | `dim_sku.VED` changed | 17 SKUs (includes 6 «Тодорхойгүй» → D) |
  | VED differs from `ROP final.xlsb` | 558 SKUs / 14,149 branch rows |
  | Status rows re-classified | 14,287 current, 43,957 history |
  | Critical V/E KPI | 5,891 → 5,412 |
  | VED-weighted gap | 73,587 → 67,296 |

  - Status, ROP and ROP gap are identical before and after.
  - A from-scratch rebuild of the current snapshot with the new VED matches the patched fact on every column (265,695 rows).
  - Reloading `ROP final.xlsb` (`update_rop.py`, run on a copy of the project) keeps the file's VED on all 14,149 rows.
- **If the workbook were recalculated with the new VED** (its rule: Z_VED = 2.326 for V, else 0), ROP would change on 1,939 rows / 108 SKUs. This is not applied.
  - 33 SKUs that became V: +480 units.
  - 75 SKUs that are no longer V: −2,759 units.
  - Total 350,105 → 347,826 (−0.65%).
- **New tab «ABC / XYZ»** (between «SKU coverage» and «Тренд»).
  - ABC × Status and XYZ × Status heatmaps side by side, each with a summary table:
    - SKU × branch rows, share of 7-month sales, ROP, stock, ROP gap, at-risk rows, covered %.
    - A НИЙТ row first.
  - Below them, a sortable SKU × branch list (first 2,000 rows) ordered «ABC-ээр» or «XYZ-ээр», with a CSV export.
  - The ABC / XYZ selectors filter this tab only; the global filters apply too. Click a cell to filter to that class and status; click again to clear. The VED heatmap now clears on a second click as well, as its help text already said.
  - ABC / XYZ are per SKU × branch (3,376 SKUs have different ABC classes at different branches), so the tab counts rows, not SKUs. A per-class distinct-SKU count would not add up to the total.
  - Queries:
    - `v_abc_xyz` is a separate view: DuckDB keeps an unused LEFT JOIN, so adding the join to `v_status` measured 8.5 → 18.6 ms on every query.
    - `DashboardQuery.class_breakdown()` is one plain `GROUP BY ABC, XYZ, Status` (38 ms vs 122 ms for two `GROUPING SETS` queries), rolled up by `MetricsCalculator.class_rollup()`. `class_items(order=...)` fills the list.
  - The three heatmaps share `status_heatmap()` / `fill_status_heatmap()` and `MetricsCalculator.heatmap_cells()`.
  - Real data:
    - A = 35,604 rows with 85.2% of sales, holding 24,609 of the 38,728 units of ROP gap; B 46,468; C 111,371.
    - X 1,652 / Y 12,658 / Z 179,133.
    - «Тодорхойгүй» = 72,252 rows without a branch ROP row (mostly «ROP байхгүй»).
- **ROP confidence filter removed** from the filter row (viewers found it confusing); `Filters.confidences` and `filter_options()["confidences"]` removed with it.
- Methodology tab: where VED comes from, the ABC / XYZ explanation.
- Backup of `data/` before the change: `_backup_before_ved_update/` (gitignored).
- Verification:
  - 116 tests passing (23 new in `tests/test_classification.py` and `test_watch_inventory.py`), including a patch-equals-rebuild test that fails when the measure recompute is disabled.
  - `warehouse.py check` passes all checks; `dagster definitions validate` passes.
  - `app.py` passes Bokeh `check_integrity` with no errors or warnings.
  - The tab was checked in Chrome: no console errors, heatmaps and summary tables side by side, totals equal to the KPI cards.
  - Tab refresh takes ~90 ms cold; a full cold `refresh_all` takes ~440 ms.

### 2026-09-21 — Dagster orchestration and the unified database
- **New `dagster_rop/` package** turning the ETL into an asset graph (see *ETL orchestration*).
  - `resources.py` — `RopProject` (paths, thresholds) and `DuckDBWarehouse` (the unified database, opened read-only).
  - `assets.py` — 3 observed sources + `branch_rop`, `inventory_snapshot`, `status_history`, `warehouse_db`.
  - `checks.py` — 5 asset checks on `warehouse_db` (3 ERROR, 2 WARN).
  - `sensors.py` — `inbox_sensor`, replacing `watch_inventory.py` as the production path.
  - `jobs.py` — 4 jobs and the weekday check schedule.
  - `definitions.py` — the entry point, configurable by environment variable.
- **`data/warehouse.duckdb` is now the single unified database**, owned by the `warehouse_db` asset: 13 tables, 1.45M rows, 40 MB. The Parquet files remain the asset files, so `app.py` and the tests are unchanged.
- **Refactor, no behaviour change:** `refresh_inventory.main()` → `refresh_inventory.refresh(...)` and `update_rop.main()` → `update_rop.load_rop(...)`, with argparse-only `main()`s. Dagster and the CLI now call one function instead of Dagster shelling out.
  - Both return a summary dict, which the assets turn into run metadata (rows, mapping rate, ROP gap, status counts).
  - `update_rop.py`'s printed summary gained `source_file`, `branch_count` and `missing_rop_sku`.
- **Single-writer safety:** `dagster_home/dagster.yaml` sets `concurrency.runs.max_concurrent_runs: 1`. Every run rewrites the same files, so concurrent runs would corrupt the warehouse. This is what the watcher's lock file and single-file queue used to guarantee.
  - The limit lives under `concurrency:`, not under `run_coordinator.config:` — the old key is accepted but silently ignored (`max_concurrent_runs` stayed `None`).
- **`dagster.yaml` storage paths are resolved against the working directory, not `DAGSTER_HOME`.** An explicit `storage.sqlite.base_dir: .` scattered `history/` and `schedules/` into the project root. Both blocks were removed; the defaults already put everything inside `DAGSTER_HOME` (`dagster_home/`), which is gitignored.
- **Bug found by the live daemon, not by the tests:** the sensor returned a bare `SkipReason` while it was waiting for a file to stop growing, which discards the cursor. The file was then unseen again on the next tick and reported "still settling" for ever. It now returns a `SensorResult` carrying both the cursor and the skip reason; `test_sensor_records_the_cursor_even_when_it_skips` covers it.
- **Dagster 1.13 gotcha:** it compares an asset's `context` annotation by identity, so `from __future__ import annotations` makes every asset fail to define with "Cannot annotate `context` parameter with type AssetExecutionContext". `assets.py` and `checks.py` carry a note saying why the import is absent.
- `dagster>=1.9` and `dagster-webserver>=1.9` added to `requirements.txt`; `workspace.yaml`, `run_dagster.bat`, `run_dagster.sh` added; `dagster_home/` ignored.
- 92 tests passing (31 new: graph shape, check classification, sensor settle / de-duplication / caps / cursor, the inventory asset's file handling, the metadata-driven checks, the resources).

### 2026-09-18 — Executive tab redesign, status-aware charts, coverage totals
- **Удирдлагын тойм**
  - The *Status бүтэц* donut is now labelled horizontal bars with count and % per status.
    - The donut was cropped and a 4-row status was invisible next to a 120k-row one.
    - Data-quality statuses (Өгөгдөл алга, ROP байхгүй) are muted.
  - The dual-axis VED chart is now a VED × Status heatmap.
    - Each cell shows the count and its share of the VED row; colour = count relative to the largest cell.
  - Click a status bar or a heatmap cell to filter to it (click again to clear).
  - New KPI card "ROP-оос илүү нөөц" = Σ (Inventory − ROP) over rows with ROP > 0.
- **Charts no longer go empty under the Илүүдэл (or any) status filter.**
  - TOP 20 has a ranking selector: Автомат / Дутагдал / Илүүдэл / Үлдэгдэл / Мэдээлэлгүй.
  - *Автомат* follows the status filter (`MetricsCalculator.auto_top_measure`):
    - at-risk statuses → VED-weighted ROP gap
    - Хэвийн / Илүүдэл → stock above ROP
    - ROP байхгүй → stock
    - Өгөгдөл алга → ROP of rows without inventory
  - TOP 20 can show SKU × branch rows or SKUs totalled across branches (`DashboardQuery.top_items`).
  - The SKU scatter's point layer shows the selected statuses (`highlight_points`), and its legend follows the statuses shown.
  - Every chart shows "Сонгосон шүүлтүүрт тохирох мэдээлэл алга" instead of blank axes when nothing matches.
- **SKU coverage tab**
  - Grouping selector: Салбар × Категори / Салбар / Категори / SKU.
  - Metric selector:
    - *Дундаж хангалт*: mean of min(Inv/ROP, 1)
    - *Хангагдсан мөр*: share of rows with Inv ≥ ROP
    - *ROP-жигнэсэн*: Σ min(Inv, ROP) / Σ ROP
  - Totals come from one DuckDB `GROUPING SETS` query (`DashboardQuery.coverage_summary`):
    - a НИЙТ KPI strip
    - НИЙТ row and column margins in the heatmap
    - a НИЙТ first row in the table
    - a dashed НИЙТ line on the bar charts
  - Branch, category and SKU bar charts are sorted worst first; SKU shows the worst 40, ties broken by ROP.
  - The table has a CSV export.
  - Coverage treats rows without inventory as zero stock. "Дутуу" = Σ ROP − Σ min(Inv, ROP), per row, no netting between SKUs.
  - The previous heatmap "Gap" netted overstock against shortage within a cell.
- **Bokeh note:** create categorical figures with `x_range=FactorRange(...)` / `y_range=FactorRange(...)` in `figure(...)`.
  - Assigning a FactorRange afterwards keeps a linear scale (validation error E-1009) and the page does not render.
  - `empty_figure(..., x_range=..., y_range=...)` now accepts ranges.
  - Verification now includes `bokeh.core.validation.check_integrity`.
- **Real data (all branches, no filter):**

  | НИЙТ | Value |
  |---|---|
  | Дундаж хангалт | 42.5% |
  | Хангагдсан мөр | 39.8% |
  | ROP-жигнэсэн | 46.0% |
  | ROP-той мөр | 136,467 |
  | Дутуу | 188,965 units |

  - Брэнд is the weakest category (10.5% average coverage).
- 62 tests passing (new: coverage totals and margins, filters, auto ranking, top items per mode).

### 2026-09-18 — First live watcher run (`Үлд 0918.csv`) and quantity check
- **First attempt failed.** The quantity column was headed `2026-09-18`, so the parser could not find an on-hand column. It succeeded after the header was renamed to `Эцсийн үлдэгдэл`.
- **The extra `Сери.Хүртэл хүчинтэй` (batch / expiry) column did not duplicate quantities.**
  - The file has one row per batch: 180,261 rows for 159,373 branch × item pairs.
  - It has no exact duplicate rows, no repeated branch + item + batch, and no subtotal rows.
  - Raw total = `fact_inventory_snapshot` total = 1,715,439.772.
  - All branch × item sums are identical, and `fact_sku_status.OnHand` equals matched inventory minus excluded branches.
- **The +43% total against 09-16 comes from the file's scope.**
  - The 74 branches present in both files sum to 1,178,953 against 1,195,051 on 09-16, a 1.3% drop.
  - The file adds 48 locations that are not in the ROP workbook (428,480 units): 30 central warehouses / reserves and 18 other locations (new pharmacies, vending points).
  - These locations load as `UNMAPPED::*` branches with status "ROP байхгүй" (13,176 rows).

### 2026-09-18 — Automatic inventory refresh (file watcher)
- Added `watch_inventory.py` (watchdog), `watch_inventory.bat`, `install_inventory_watcher.ps1` (Task Scheduler at logon, restart on failure) and `tests/test_watch_inventory.py` (22 tests).
- `watchdog>=4` added to `requirements.txt`; `inbox/` and `logs/` added to `.gitignore`.
- Verified end to end on a copy of the project using a realistic 137k-row export:
  - The file was detected, refreshed and checked in 15 s, and snapshot 2026-09-19 appeared in history.
  - A copy with identical content was skipped.
  - An Excel lock file was ignored.
  - A corrupt file went to `failed/` with its error log.
  - A second watcher instance was refused.

### 2026-09-18 — Repository set up
- Added `.gitignore`. Committed the original package as a baseline on `master` and the refactor on `star-schema-refactor`.
- `README_MN.md` and `powerbi/` (DAX, Power Query, layout docs) were deleted on 2026-09-23; they described the old wide CSVs. They remain in the baseline commit's history.

### 2026-09-18 — Star-schema refactor
- Added `warehouse.py`, `queries.py`, `metrics.py`, `tests/`; `app.py` is now UI only.
- Moved storage from CSV.gz to Parquet + DuckDB (`fact_sku_status` 15.9 MB → 1.8 MB). Old files backed up in `_backup_before_star_schema/`.
- `SKU category.csv` now drives Category: 6,657 SKUs matched, 124 kept their old category (`data/category_review.csv`). "Экс" → "Эксклюзив"; new "Брэнд" category.
- New **Тренд** tab: status mix per snapshot, at-risk / critical V/E / ROP gap lines, deltas vs previous snapshot, and a two-date status movement table with CSV export. Uploads in the app are added to history.
- Performance: status rebuild 13.6 s → ~1 s (identical output); threshold change ~1.7 s; filter change ~130 ms.
- Fixes:
  - `build_model.py` missing imports.
  - Crash when accepting a mapping (deleted `powerbi_data/`); Power BI export is now opt-in (`--powerbi`).
  - Duplicate aliases on accept.
  - 24 duplicated SKU × branch rows in the ROP file were double-counted (Inventory Position −96, Total ROP −3 after fix).
  - Duplicate 2026-09-07 snapshot (misnamed file) de-duplicated in history.
  - "Treat missing as zero" did not update Critical V/E.
  - Old snapshots stored CoverageRatio above 1; now capped at 1 like the current definition.

## Known issues / notes
- `bokeh serve` reads `app.py` once at start-up. After changing any `.py` file, restart the server; reloading the page is not enough (or run with `--dev`).
- Screenshot / automation tools cannot render a Chrome tab whose window is minimised or covered (`document.visibilityState == "hidden"`). This looks like a frozen page but is not.
- Inventory exports that include central warehouses ("… агуулах", "… нөөц") inflate the Inventory Position KPI and the "ROP байхгүй" count.
  - Undecided: exclude warehouses from the dashboard, or keep them as a separate branch group.
- Exports whose quantity column is headed with a date (e.g. `2026-09-18`) are not recognised by `parse_inventory`; the header must be renamed to `Үлдэгдэл` / `Эцсийн үлдэгдэл`.
- Snapshots 2026-08-31 and 2026-09-02 contain identical data (flat trend between them).
- 2026-08-13 is a NETWORK-scope snapshot; the Trend tab hides it unless the NETWORK checkbox is ticked, and it cannot be compared SKU × branch with later dates.
- In-app upload takes ~23 s, mostly writing the compressed snapshot files.
- `QA_REPORT.txt` is stale output from the original package; `QA_REPORT.json` is current.
- Do not run `watch_inventory.py` and the Dagster `inbox_sensor` at the same time — both would claim the same file in `inbox/`. Pick one.
- `dagster dev` does not reliably reload a changed module in the code server on Windows; restart it after editing anything under `dagster_rop/`.
- Do not set `storage.sqlite.base_dir` in `dagster_home/dagster.yaml`; it is relative to the working directory and drops `history/` and `schedules/` wherever the command was run. The defaults are `DAGSTER_HOME`-relative and correct.
- The `inbox_sensor` needs two ticks (~60 s) before it launches a run, by design: one tick of unchanged size and mtime is how it knows the copy has finished. The old watcher used a 5 s settle window instead.
- Both `warehouse.connect()` (Parquet, used by the dashboard) and `data/warehouse.duckdb` (used by external tools) exist. The DuckDB file is only as fresh as the last `warehouse_db` materialisation or `warehouse.py build`.
- Run-status sensors for success / failure notification are not written yet; the watcher's `--on-success` / `--on-failure` hooks have no Dagster equivalent in place.
- **The sensor and the schedule ship stopped and must be enabled once** (UI → Automation, or `dagster sensor start -m dagster_rop.definitions inbox_sensor`). Until then a file dropped in `inbox/` just sits there — there is no error, because nothing is watching.
- **Autostart since 2026-09-22** (reverses the 2026-09-21 decision). The «ROP Dagster» logon task (`install_dagster_autostart.ps1`) runs `dagster_supervisor.ps1`, which keeps Dagster running hidden and restarts it within a minute if it stops; it waits while a Dagster started by hand holds port 3000. The task also re-launches the supervisor every 5 minutes if it has ended (2026-09-23). `install_dagster_autostart.ps1 -Status` shows the state; logs in `logs/dagster*.log`.
- The sensor needs ~30-60 s to pick a file up, against the watcher's ~5 s. One tick of unchanged size and mtime is how it knows the copy finished.
- `inbox/.watcher_state.json` is a leftover from the watcher. It is harmless, but it is a reminder not to run `watch_inventory.py` and `inbox_sensor` together.
- **ROP was calculated with the workbook's VED, not the VED file's** (2026-09-21).
  - For 108 SKUs / 1,939 branch rows the displayed VED and the VED behind ROP / Z_VED differ in a way that matters: 33 SKUs are now V but have a non-V ROP (+480 units if recalculated), and 75 SKUs are no longer V (−2,759).
  - Undecided: update the VED column in `ROP final.xlsb` and reload, or recalculate ROP in the ETL.
  - `data/ved_review.csv` («ROP файлын VED-ээс өөр») lists all 558 SKUs whose VED differs from the workbook.
- 1,884 names in `VED ангилал.csv` match no SKU. 1,771 of them are products in the inventory export with no SKU in the ROP master, so they are not on the dashboard at all.
- 953 SKUs are not in `VED ангилал.csv` and keep their earlier VED (`VED_Source` = "ROP мастер").
- SKU 3445: the alias table maps «Cumlaude lab үтрээний гүн чийгшүүлэх лаа» (a suppository, VED E) to this SKU, which is the gel «Cumlaude lab MD Үтрээ чийгшүүлэх гель…» (VED D). The SKU's own name wins (D); the alias looks wrong.
- The ROP confidence *filter* is gone, but ROP confidence still appears in the SKU table's «Confidence» column, the SKU detail line, the «Low-confidence ROP» bar on the data-quality tab and the methodology text.
- `Үлд 0921.csv` (2026-09-21) failed twice in the Dagster inbox run (11:44, 12:36; now in `inbox/failed/`). Its quantity column is headed `2026-09-21` — the known date-header limitation above. The dashboard still shows the 2026-09-18 snapshot.
- After this change the running `bokeh serve` and `dagster dev` hold the old code; restart both.
