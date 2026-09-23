"""Resources: where the project lives and how the unified DuckDB is reached.

The warehouse itself stays exactly where it is — ``data/*.parquet`` are the
asset files and ``data/warehouse.duckdb`` is the one unified database. Dagster
does not introduce a second copy of the data; it owns *when* those files are
rewritten and records what came out.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import duckdb
from dagster import ConfigurableResource

PROJECT_DIR = Path(__file__).resolve().parent.parent


class RopProject(ConfigurableResource):
    """Paths to the project's sources and outputs."""

    project_dir: str = str(PROJECT_DIR)
    rop_workbook: str = "ROP final.xlsb"
    category_csv: str = "SKU category.csv"
    ved_csv: str = "VED ангилал.csv"
    abc_xyz_workbook: str = "ABC XYZ.xlsx"
    expiry_config: str = "expiry_config.json"
    inbox_dir: str = "inbox"
    excess_threshold: float = 2.0
    missing_as_zero: bool = False
    powerbi_export: bool = False

    @property
    def root(self) -> Path:
        return Path(self.project_dir)

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def snapshot_dir(self) -> Path:
        return self.root / "snapshots"

    @property
    def rop_path(self) -> Path:
        return self.root / self.rop_workbook

    @property
    def category_path(self) -> Path:
        return self.root / self.category_csv

    @property
    def ved_path(self) -> Path:
        return self.root / self.ved_csv

    @property
    def abc_xyz_path(self) -> Path:
        return self.root / self.abc_xyz_workbook

    @property
    def expiry_config_path(self) -> Path:
        return self.root / self.expiry_config

    @property
    def inbox(self) -> Path:
        return self.root / self.inbox_dir

    @property
    def processed_dir(self) -> Path:
        return self.inbox / "processed"

    @property
    def failed_dir(self) -> Path:
        return self.inbox / "failed"

    def ensure_inbox(self) -> None:
        for d in (self.inbox, self.processed_dir, self.failed_dir):
            d.mkdir(parents=True, exist_ok=True)


class DuckDBWarehouse(ConfigurableResource):
    """The unified database: ``data/warehouse.duckdb``.

    Opened read-only for queries so a running dashboard or DBeaver session
    cannot be locked out, and so a query can never corrupt the file. The
    writing path stays ``warehouse.build_duckdb_file()``, which writes a
    temp file and renames it into place.
    """

    database: str = str(PROJECT_DIR / "data" / "warehouse.duckdb")

    @property
    def path(self) -> Path:
        return Path(self.database)

    def connect(self, read_only: bool = True) -> Iterator[duckdb.DuckDBPyConnection]:
        return duckdb.connect(str(self.path), read_only=read_only)

    def query_one(self, sql: str) -> Any:
        con = duckdb.connect(str(self.path), read_only=True)
        try:
            row = con.execute(sql).fetchone()
            return row[0] if row else None
        finally:
            con.close()

    def table_rows(self) -> dict[str, int]:
        """Row count per table in the unified database."""
        con = duckdb.connect(str(self.path), read_only=True)
        try:
            names = [r[0] for r in con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main' ORDER BY table_name").fetchall()]
            return {n: con.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0] for n in names}
        finally:
            con.close()
