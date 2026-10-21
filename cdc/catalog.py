"""One Iceberg catalog for the whole platform.

Both the batch publisher (`transform/spark/iceberg_publish.py`) and the CDC consumer
(`cdc/consumer.py`) must resolve the same catalog file and warehouse directory,
otherwise a reader looking at `silver.books` and a reader looking at
`cdc.product_price_current` would be talking to two different metadata stores and
"the platform" would be two platforms.

The path handling is the part that actually breaks on Windows, so it lives in one
place: see the docstring in `transform/spark/iceberg_publish.build_catalog`.
"""

from __future__ import annotations

from pathlib import Path

from pyiceberg.catalog.sql import SqlCatalog

from tools.common.config import get_settings

CATALOG_NAME = "sentinel"


def catalog_paths() -> tuple[Path, Path]:
    """Return (catalog database file, warehouse directory), creating both parents."""
    base = get_settings().root / "data" / "iceberg"
    warehouse = base / "warehouse"
    warehouse.mkdir(parents=True, exist_ok=True)
    return base / "catalog.db", warehouse


def build_catalog() -> SqlCatalog:
    """A SQLite-backed catalog over a filesystem warehouse.

    The warehouse is passed as a path *relative to the repository root* on purpose:
    pyiceberg turns `file:///F:/dir` into `/F:/dir`, which pyarrow rejects with
    WinError 123, and a bare `F:/dir` makes urlparse read `f` as a URI scheme. A
    relative path has no scheme, so the local filesystem is used.
    """
    db_file, warehouse = catalog_paths()
    relative_warehouse = warehouse.relative_to(get_settings().root).as_posix()
    return SqlCatalog(
        CATALOG_NAME,
        **{
            "uri": f"sqlite:///{db_file.as_posix()}",
            "warehouse": relative_warehouse,
        },
    )
