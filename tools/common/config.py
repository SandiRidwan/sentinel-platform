"""Shared configuration for every SENTINEL component.

One place reads the environment so Scrapy, dbt, Spark, Airflow and the task runner
can never disagree about where the bucket or the warehouse lives.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


@dataclass(frozen=True)
class S3Settings:
    endpoint_url: str
    access_key_id: str
    secret_access_key: str
    region: str
    force_path_style: bool
    bucket_raw: str
    bucket_bronze: str
    bucket_silver: str

    @property
    def buckets(self) -> tuple[str, str, str]:
        return (self.bucket_raw, self.bucket_bronze, self.bucket_silver)


@dataclass(frozen=True)
class PostgresSettings:
    host: str
    port: int
    db: str
    user: str
    password: str
    metabase_db: str = "metabase"

    @property
    def dsn(self) -> str:
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.db}"

    @property
    def psycopg_dsn(self) -> str:
        return f"host={self.host} port={self.port} dbname={self.db} user={self.user} password={self.password}"


@dataclass(frozen=True)
class ClickHouseSettings:
    host: str
    port: int
    db: str
    user: str
    password: str

    @property
    def http_url(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass(frozen=True)
class ScrapeSettings:
    user_agent: str
    max_concurrency: int
    download_delay: float
    obey_robots: bool
    go_fetcher_url: str


@dataclass(frozen=True)
class GovernanceSettings:
    pii_mask_enabled: bool
    retention_days_raw: int
    retention_days_gold: int
    openlineage_url: str


@dataclass(frozen=True)
class Settings:
    root: Path
    env: str
    s3: S3Settings
    pg: PostgresSettings
    ch: ClickHouseSettings
    scrape: ScrapeSettings
    governance: GovernanceSettings
    dbt_target: str
    duckdb_path: Path
    extra: dict[str, str] = field(default_factory=dict)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load .env once and cache. Call get_settings.cache_clear() in tests."""
    load_dotenv(ROOT / ".env", override=False)

    duckdb_path = Path(os.environ.get("DUCKDB_PATH", "./data/warehouse/sentinel.duckdb"))
    if not duckdb_path.is_absolute():
        duckdb_path = (ROOT / duckdb_path).resolve()

    return Settings(
        root=ROOT,
        env=os.environ.get("SENTINEL_ENV", "local"),
        s3=S3Settings(
            endpoint_url=os.environ.get("S3_ENDPOINT_URL", "http://localhost:9090"),
            access_key_id=os.environ.get("S3_ACCESS_KEY_ID", "sentinel"),
            secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY", "sentinel"),
            region=os.environ.get("S3_REGION", "us-east-1"),
            force_path_style=_bool("S3_FORCE_PATH_STYLE", True),
            bucket_raw=os.environ.get("S3_BUCKET_RAW", "sentinel-raw"),
            bucket_bronze=os.environ.get("S3_BUCKET_BRONZE", "sentinel-bronze"),
            bucket_silver=os.environ.get("S3_BUCKET_SILVER", "sentinel-silver"),
        ),
        pg=PostgresSettings(
            host=os.environ.get("POSTGRES_HOST", "localhost"),
            port=_int("POSTGRES_PORT", 5432),
            db=os.environ.get("POSTGRES_DB", "sentinel"),
            user=os.environ.get("POSTGRES_USER", "sentinel"),
            password=os.environ.get("POSTGRES_PASSWORD", "change-me-postgres"),
            metabase_db=os.environ.get("METABASE_DB", "metabase"),
        ),
        ch=ClickHouseSettings(
            host=os.environ.get("CLICKHOUSE_HOST", "localhost"),
            port=_int("CLICKHOUSE_PORT", 8123),
            db=os.environ.get("CLICKHOUSE_DB", "sentinel"),
            user=os.environ.get("CLICKHOUSE_USER", "default"),
            password=os.environ.get("CLICKHOUSE_PASSWORD", "change-me-clickhouse"),
        ),
        scrape=ScrapeSettings(
            user_agent=os.environ.get(
                "SCRAPE_USER_AGENT",
                "SENTINEL/1.0 (+https://github.com/SandiRidwan/sentinel-platform)",
            ),
            max_concurrency=_int("SCRAPE_MAX_CONCURRENCY", 8),
            download_delay=_float("SCRAPE_DOWNLOAD_DELAY", 1.0),
            obey_robots=_bool("SCRAPE_OBEY_ROBOTS", True),
            go_fetcher_url=os.environ.get("GO_FETCHER_URL", "http://localhost:8088"),
        ),
        governance=GovernanceSettings(
            pii_mask_enabled=_bool("PII_MASK_ENABLED", True),
            retention_days_raw=_int("RETENTION_DAYS_RAW", 90),
            retention_days_gold=_int("RETENTION_DAYS_GOLD", 730),
            openlineage_url=os.environ.get("OPENLINEAGE_URL", "http://localhost:5000"),
        ),
        dbt_target=os.environ.get("DBT_TARGET", "dev"),
        duckdb_path=duckdb_path,
    )
