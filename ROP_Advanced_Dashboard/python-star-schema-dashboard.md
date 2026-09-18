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

**Last updated**: 2026-09-18

## Decisions
| Topic | Guide suggests | Chosen for this project | Why |
|---|---|---|---|
| Dashboard framework | Streamlit / Dash | **Bokeh server** (kept), refactored into layers | Existing 6-tab app worked; lower risk than a rewrite |
| Warehouse | Snowflake / PostgreSQL / DuckDB | **DuckDB + Parquet** (`data/*.parquet`) | Embedded, no server, fast enough for ~1.1M history rows |
| Category source | — | **`SKU category.csv`** overrides `dim_sku.Category` | Business-owned category list |
| History | Time dimension (`dim_date`) | **`fact_status_history`** keyed by `DateKey` + Trend tab | Snapshots existed but were never used |

## Architecture (as built)
```
Source files (Excel / CSV, snapshots/status_*.csv.gz)
        ↓  build_model.py · update_rop.py · refresh_inventory.py · warehouse.py build
data/*.parquet  (+ data/warehouse.duckdb for external SQL tools)
        ↓  warehouse.connect()  — in-memory DuckDB, tables loaded at app start
queries.py      DashboardQuery + Filters  — SQL star joins, filters in SQL, cached per data version
        ↓
metrics.py      MetricsCalculator  — pure calculations (KPIs, coverage, hexbin, trend table)
        ↓
app.py          Bokeh UI  — widgets, figures, callbacks only
```

## Star schema
| Table | Grain / role | Rows (2026-09-18) |
|---|---|---|
| `dim_sku` | SKU master; Category from `SKU category.csv` | 6,781 |
| `dim_branch` | Branch; flags `IsNetwork`, `IsMapped` (UNMAPPED::*), `IsExcluded` | 94 |
| `dim_date` | Day; `DateKey` yyyymmdd, `HasSnapshot` | 1,095 |
| `dim_status`, `dim_ved`, `dim_category` | Lookup / ordering (`Severity` used for movement) | 6 / 4 / 6 |
| `fact_branch_rop` | SKU × branch ROP parameters | 193,469 |
| `fact_sku_status` | SKU × branch × date — current snapshot | 252,657 |
| `fact_status_history` | SKU × branch × date — all snapshots | 1,147,960 |
| `fact_inventory_snapshot` | Inventory line | 137,379 |

Fact tables hold keys, measures and degenerate attributes only (Status, VED, Supplier…); names, categories and branch names come from dimensions via `v_status` / `v_history` views.

## Guide checklist
- [x] Fact tables slimmed to keys + measures; dimensions denormalized
- [x] Query builder pattern — `queries.DashboardQuery`
- [x] Dimension filtering in SQL, parameterized — `queries.Filters.where()`
- [x] Caching — per `(method, filters, data version)`; cleared on upload / threshold change / reset
- [x] Metrics class — `metrics.MetricsCalculator`
- [x] Columnar storage — Parquet
- [x] Referential integrity tests — `tests/test_star_schema.py`, `warehouse.integrity_checks()`
- [x] Metric calculation tests — `tests/test_metrics.py` (30 tests total, all passing)
- [x] Time dimension used — Trend tab + snapshot comparison
- [ ] Slowly changing dimensions (history uses current `dim_sku`, e.g. today's Category, for past dates) — SCD Type 1 for now
- [ ] Authentication / deployment beyond local `bokeh serve`
- [ ] Scheduled refresh uses `schedule_refresh.ps1` → `refresh_inventory.py` (unchanged; not re-tested)

## Commands
| Task | Command |
|---|---|
| Install dependencies | `pip install -r requirements.txt` |
| Rebuild warehouse (categories, history, checks) | `python warehouse.py build --categories "SKU category.csv"` |
| Rebuild history only | `python warehouse.py history` |
| Integrity checks | `python warehouse.py check` or `python qa_check.py` |
| New inventory file | `python refresh_inventory.py --inventory <file> --snapshot-date YYYY-MM-DD` |
| New ROP workbook | `python update_rop.py --rop "ROP final.xlsb"` |
| Power BI CSV export | add `--powerbi` to any of the above |
| Tests | `python -m pytest tests -q` |
| Run dashboard | `run_dashboard.bat` (`python -m bokeh serve --show app.py`) |

## Repository
- Git root is the parent folder `ROP_Advanced_Dashboard_Package (1)`.
- `master`: baseline commit of the original package.
- `star-schema-refactor`: the refactor described below.
- `.gitignore` keeps these out of Git; they stay on disk only:
  - business data: `data/`, `snapshots/`, `powerbi_data/`, `*.xlsb`, `*.xlsx`
  - generated files: `*.duckdb`, dashboard previews, `QA_REPORT.txt`
  - backups and `__pycache__/`
- A fresh clone needs the source files plus `python warehouse.py build` before the app can run.

## Change log
### 2026-09-18 — Repository set up
- Added `.gitignore`. Committed the original package as a baseline on `master` and the refactor on `star-schema-refactor`.
- Deleting `README_MN.md` and `powerbi/` (DAX, Power Query, layout docs) is still uncommitted, pending a decision. The Power BI docs describe the old wide CSVs and would need updating if kept.

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
- Snapshots 2026-08-31 and 2026-09-02 contain identical data (flat trend between them).
- 2026-08-13 is a NETWORK-scope snapshot; the Trend tab hides it unless the NETWORK checkbox is ticked, and it cannot be compared SKU × branch with later dates.
- In-app upload takes ~23 s, mostly writing the compressed snapshot files.
- `QA_REPORT.txt` is stale output from the original package; `QA_REPORT.json` is current.
