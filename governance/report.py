"""Turn the control register into an evidence pack.

    python -m governance.report
    python -m governance.report --fail-on partial

Outputs:
    quality/reports/controls-report.json   machine-readable, one entry per control
    quality/reports/controls-report.csv    the shape a reviewer or a spreadsheet expects

Two design choices worth stating, because they are what make this useful to someone
else rather than to the person who wrote it:

  * Evidence is *checked*, not copied. Every entry in controls.yml is evaluated against
    the file it names, so a control cannot drift into being false while the register
    still says `implemented`.

  * Gaps are first-class. `status: gap` with a remediation note is a normal outcome. A
    register with no gaps is either a very small system or a register that is lying.

Exit codes:
    0  every `implemented` control has passing evidence
    1  an implemented control has missing or failing evidence, or no evidence at all
    2  the register itself is malformed
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
REGISTER = ROOT / "governance" / "controls.yml"
JSON_REPORT = ROOT / "quality" / "reports" / "controls-report.json"
CSV_REPORT = ROOT / "quality" / "reports" / "controls-report.csv"

VALID_STATUS = {"implemented", "partial", "gap"}


def resolve(path: str) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else ROOT / candidate


def walk(data: Any, dotted: str) -> Any:
    current = data
    for part in dotted.split("."):
        if isinstance(current, list):
            try:
                current = current[int(part)]
                continue
            except (ValueError, IndexError):
                return None
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def check(entry: dict) -> dict:
    """Evaluate one evidence entry. Returns {ok, kind, path, detail, hint?}."""
    kind = entry.get("type")
    path = entry.get("path", "")
    target = resolve(path)

    # Reports under quality/reports are generated, not committed. On a fresh clone the
    # first run of this command therefore fails — correctly, because there is no evidence
    # yet — but the message has to say which command produces the missing file. A bare
    # "file not found" sends people looking for a deleted file instead of a missing run.
    hint = ""
    if path.startswith("quality/reports/") and not target.exists():
        hint = "run the pipeline that produces it first: make quality / make agent-eval / make cdc"

    if kind == "file":
        if not target.exists():
            return {"ok": False, "kind": kind, "path": path, "detail": "file not found", "hint": hint}
        minimum = entry.get("min_bytes")
        size = target.stat().st_size
        if minimum is not None and size < minimum:
            return {"ok": False, "kind": kind, "path": path, "detail": f"{size} bytes < {minimum}"}
        return {"ok": True, "kind": kind, "path": path, "detail": f"{size} bytes"}

    if kind == "text_contains":
        if not target.exists():
            return {"ok": False, "kind": kind, "path": path, "detail": "file not found", "hint": hint}
        pattern = entry.get("pattern", "")
        text = target.read_text(encoding="utf-8", errors="replace")
        if re.search(pattern, text, flags=re.MULTILINE):
            return {"ok": True, "kind": kind, "path": path, "detail": f"matched /{pattern}/"}
        return {"ok": False, "kind": kind, "path": path, "detail": f"no match for /{pattern}/"}

    if kind == "json_field":
        if not target.exists():
            return {"ok": False, "kind": kind, "path": path, "detail": "file not found", "hint": hint}
        try:
            data = json.loads(target.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            return {"ok": False, "kind": kind, "path": path, "detail": f"invalid JSON: {exc}"}
        field = entry.get("field", "")
        value = walk(data, field)
        if "equals" in entry:
            expected = entry["equals"]
            ok = value == expected
            return {
                "ok": ok,
                "kind": kind,
                "path": path,
                "detail": f"{field}={value!r} (expected {expected!r})",
            }
        return {"ok": value is not None, "kind": kind, "path": path, "detail": f"{field}={value!r}"}

    if kind == "jsonl_min_rows":
        if not target.exists():
            return {"ok": False, "kind": kind, "path": path, "detail": "file not found", "hint": hint}
        rows = sum(1 for line in target.read_text(encoding="utf-8").splitlines() if line.strip())
        minimum = int(entry.get("min_rows", 1))
        return {
            "ok": rows >= minimum,
            "kind": kind,
            "path": path,
            "detail": f"{rows} rows (minimum {minimum})",
        }

    return {"ok": False, "kind": str(kind), "path": path, "detail": f"unknown evidence type {kind!r}"}


def evaluate(register: dict) -> dict:
    controls = register.get("controls") or []
    results = []
    implemented_without_evidence = 0
    failing = 0
    gaps = 0
    partial = 0

    for control in controls:
        status = control.get("status")
        if status not in VALID_STATUS:
            raise SystemExit(f"control {control.get('id')} has invalid status {status!r}")

        checks = [check(entry) for entry in (control.get("evidence") or [])]
        evidence_ok = bool(checks) and all(item["ok"] for item in checks)

        if status == "implemented":
            if not checks:
                implemented_without_evidence += 1
            elif not evidence_ok:
                failing += 1
        elif status == "gap":
            gaps += 1
        elif status == "partial":
            partial += 1

        results.append(
            {
                "id": control.get("id"),
                "name": control.get("name"),
                "status": status,
                "frameworks": control.get("frameworks") or [],
                "owner": control.get("owner", ""),
                "remediation": control.get("remediation", ""),
                "evidence_ok": evidence_ok,
                "evidence": checks,
            }
        )

    passed = sum(1 for item in results if item["status"] == "implemented" and item["evidence_ok"])
    return {
        "controls": results,
        "summary": {
            "total": len(results),
            "implemented": sum(1 for item in results if item["status"] == "implemented"),
            "partial": partial,
            "gap": gaps,
            "implemented_with_passing_evidence": passed,
            "implemented_with_failing_evidence": failing,
            "implemented_without_evidence": implemented_without_evidence,
        },
    }


def write_csv(results: dict) -> None:
    """A reviewer's format.

    The JSON is for machines; the CSV is what gets pasted into a spreadsheet during an
    audit. Writing only the JSON assumes the reader will parse it, and they will not.
    """
    CSV_REPORT.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["id", "control", "status", "evidence_ok", "evidence_files", "frameworks", "owner", "remediation"])
    for item in results["controls"]:
        writer.writerow(
            [
                item["id"],
                item["name"],
                item["status"],
                item["evidence_ok"],
                "; ".join(sorted({entry["path"] for entry in item["evidence"]})),
                "; ".join(item["frameworks"]),
                item["owner"],
                item["remediation"],
            ]
        )
    CSV_REPORT.write_text(buffer.getvalue(), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the control register into an evidence pack.")
    parser.add_argument(
        "--fail-on",
        choices=["none", "partial"],
        default="none",
        help="'partial' also fails when any control is only partially implemented",
    )
    args = parser.parse_args(argv)

    if not REGISTER.exists():
        raise SystemExit(f"control register not found: {REGISTER}")

    register = yaml.safe_load(REGISTER.read_text(encoding="utf-8"))
    if not isinstance(register, dict) or "controls" not in register:
        raise SystemExit("control register is malformed: expected a `controls` list")

    results = evaluate(register)
    summary = results["summary"]

    report = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "register": REGISTER.relative_to(ROOT).as_posix(),
        "register_reviewed_at": register.get("reviewed_at"),
        **results,
        "result": "PASS" if summary["implemented_with_failing_evidence"] == 0 and summary["implemented_without_evidence"] == 0 else "FAIL",
    }

    JSON_REPORT.parent.mkdir(parents=True, exist_ok=True)
    JSON_REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    write_csv(results)

    for item in results["controls"]:
        mark = "OK  " if item["evidence_ok"] else ("GAP " if item["status"] == "gap" else "FAIL")
        print(f"  [{mark}] {item['id']}  {item['name']}  ({item['status']})")
        if not item["evidence_ok"]:
            seen_hints: set[str] = set()
            for entry in item["evidence"]:
                if not entry["ok"]:
                    print(f"          {entry['path']}: {entry['detail']}")
                    hint = entry.get("hint")
                    if hint and hint not in seen_hints:
                        seen_hints.add(hint)
                        print(f"          -> {hint}")

    print()
    print(f"implemented            : {summary['implemented_with_passing_evidence']}/{summary['implemented']}")
    print(f"partial                : {summary['partial']}")
    print(f"declared gaps          : {summary['gap']}")
    print(f"failing evidence       : {summary['implemented_with_failing_evidence']}")
    print(f"missing evidence       : {summary['implemented_without_evidence']}")
    print(f"RESULT                 : {report['result']}")
    print(f"reports                : {JSON_REPORT.relative_to(ROOT).as_posix()}, {CSV_REPORT.relative_to(ROOT).as_posix()}")

    if report["result"] == "FAIL":
        return 1
    if args.fail_on == "partial" and summary["partial"]:
        print("RESULT (strict)        : FAIL (partial controls present)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
