"""Source register loader and gate.

Two questions this answers:
  1. May we crawl this host at all?          -> assert_approved(host)
  2. What rate does the register allow?      -> rate_limit_for(host)

The register is data, not code, so a reviewer can audit what the platform targets without
reading spiders.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from tools.common.config import ROOT


class SourceNotApproved(RuntimeError):
    """Raised when a request targets a host the register does not approve."""


def _normalise_host(host: str) -> str:
    """Lowercase, strip the port, and drop a leading `www.`."""
    bare = host.lower().split(":")[0].strip()
    return bare[4:] if bare.startswith("www.") else bare


@dataclass(frozen=True)
class Source:
    key: str
    host: str
    display_name: str
    purpose: str
    robots_status: str
    legal_basis: str
    approved: bool
    rate_limit_rps: float
    allowed_paths: tuple[str, ...]
    notes: str = ""


@dataclass(frozen=True)
class RejectedSource:
    host: str
    reason: str


@dataclass(frozen=True)
class Register:
    version: int
    reviewed_at: str
    sources: dict[str, Source]
    rejected: tuple[RejectedSource, ...]

    def by_host(self, host: str) -> Source | None:
        """Match a request host against the register, tolerating a leading `www.`.

        A site is one site whether it is reached as example.com or www.example.com, and
        forcing the register to list both invites them drifting apart.
        """
        host = _normalise_host(host)
        for source in self.sources.values():
            if _normalise_host(source.host) == host:
                return source
        return None

    def approved_hosts(self) -> tuple[str, ...]:
        return tuple(s.host for s in self.sources.values() if s.approved)


def register_path() -> Path:
    return ROOT / "governance" / "sources.yml"


@lru_cache(maxsize=1)
def load_register(path: Path | None = None) -> Register:
    raw = yaml.safe_load((path or register_path()).read_text(encoding="utf-8"))
    sources = {
        key: Source(
            key=key,
            host=entry["host"],
            display_name=entry.get("display_name", entry["host"]),
            purpose=entry.get("purpose", ""),
            robots_status=entry.get("robots_status", "restricted"),
            legal_basis=entry.get("legal_basis", "unknown"),
            approved=bool(entry.get("approved", False)),
            rate_limit_rps=float(entry.get("rate_limit_rps", 0.0)),
            allowed_paths=tuple(entry.get("allowed_paths", [])),
            notes=entry.get("notes", ""),
        )
        for key, entry in (raw.get("sources") or {}).items()
    }
    rejected = tuple(
        RejectedSource(host=entry["host"], reason=entry.get("reason", ""))
        for entry in (raw.get("rejected") or [])
    )
    return Register(
        version=int(raw.get("version", 1)),
        reviewed_at=str(raw.get("reviewed_at", "")),
        sources=sources,
        rejected=rejected,
    )


def assert_approved(host: str) -> Source:
    """Return the approved Source for a host, or raise with the reason it is refused."""
    register = load_register()
    source = register.by_host(host)

    if source is None:
        for rejected in register.rejected:
            if rejected.host.lower() in host.lower():
                raise SourceNotApproved(
                    f"host '{host}' is on the rejected list: {rejected.reason}"
                )
        raise SourceNotApproved(
            f"host '{host}' is not declared in {register_path().name}; "
            "add it with a legal basis before crawling"
        )

    if not source.approved:
        raise SourceNotApproved(
            f"host '{host}' is declared but not approved "
            f"(robots_status={source.robots_status})"
        )
    return source


def rate_limit_for(host: str) -> float:
    source = load_register().by_host(host)
    return source.rate_limit_rps if source else 0.0


def is_path_allowed(host: str, path: str) -> bool:
    # robots.txt is always permitted. Refusing it would be absurd: reading the file that
    # states the rules is the opposite of violating them, and Scrapy fetches it before
    # the first real request when ROBOTSTXT_OBEY is on.
    if path.rstrip("/") in {"/robots.txt", "/sitemap.xml"}:
        return True

    source = load_register().by_host(host)
    if source is None or not source.approved:
        return False
    if not source.allowed_paths:
        return True
    return any(path.startswith(prefix) for prefix in source.allowed_paths)
