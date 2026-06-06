"""
pop.monitor
~~~~~~~~~~~
Monthly POP scoring monitor — orchestrates the full cycle:

  1. Poll ServiceNow CMDB for all active application CIs
  2. Pull recent change/deployment records (default: last 35 days)
  3. For each changed or new app, initiate a DB metadata scan
  4. Merge CMDB metadata + DB scan results into a scoring row
  5. Run the POP scorer
  6. Write scores back to ServiceNow (if write_back.enabled = true)
  7. Emit a ranked CSV report and a JSON run-summary for audit

Designed to be triggered by:
  - A ServiceNow Scheduled Job (monthly)
  - A CI/CD pipeline cron step
  - Manually: python -m pop.cli monitor --run-now

All behaviour is driven by config.yaml — no hardcoded logic here.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

from pop.connectors.servicenow import ServiceNowClient, ApplicationCI
from pop.connectors.db_scanner import DBScanner, DBScanResult
from pop.scorer import compute_score, validate_config, ScoreResult


# ── Run summary ───────────────────────────────────────────────────────────────

@dataclass
class MonitorRunSummary:
    run_id: str
    started_at: str
    finished_at: str
    apps_found: int
    apps_scanned: int         # had a DB scan triggered (changed or force_all)
    apps_skipped: int         # not changed this cycle — score carried forward
    results: list[ScoreResult]
    scan_warnings: dict[str, list[str]] = field(default_factory=dict)  # app_name → warnings
    config_issues: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def tier_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.results:
            counts[r.tier] = counts.get(r.tier, 0) + 1
        return counts


# ── Monitor ───────────────────────────────────────────────────────────────────

class POPMonitor:
    """
    Orchestrates one full scoring cycle.

    Usage:
        monitor = POPMonitor(config)
        summary = monitor.run()
    """

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.sn = ServiceNowClient(config["servicenow"])
        self.scanner = DBScanner(config.get("db_scanner", {}))
        self.monitor_cfg = config.get("monitor", {})

    def run(self, force_all: bool = False) -> MonitorRunSummary:
        """
        Execute one full monitor cycle.

        force_all=True rescans every app regardless of change records.
        By default, only apps with recent changes are rescanned.
        """
        run_id = datetime.now(timezone.utc).strftime("pop-run-%Y%m%d-%H%M%S")
        started_at = datetime.now(timezone.utc).isoformat()
        errors: list[str] = []

        # ── 1. Validate config ─────────────────────────────────────────────
        config_issues = validate_config(self.config)

        # ── 2. Pull CIs from ServiceNow ────────────────────────────────────
        _log(f"[{run_id}] Fetching application CIs from ServiceNow…")
        try:
            applications = self.sn.get_applications()
        except Exception as exc:
            errors.append(f"ServiceNow fetch failed: {exc}")
            return MonitorRunSummary(
                run_id=run_id, started_at=started_at,
                finished_at=datetime.now(timezone.utc).isoformat(),
                apps_found=0, apps_scanned=0, apps_skipped=0,
                results=[], errors=errors, config_issues=config_issues,
            )

        _log(f"[{run_id}] Found {len(applications)} active CIs.")

        # ── 3. Pull recent changes ─────────────────────────────────────────
        lookback = self.monitor_cfg.get("lookback_days", 35)
        if force_all:
            changed_sys_ids = {a.sys_id for a in applications}
            _log(f"[{run_id}] force_all=True — scanning all {len(changed_sys_ids)} apps.")
        else:
            _log(f"[{run_id}] Checking for changes in last {lookback} days…")
            try:
                changed_sys_ids = self.sn.get_recent_changes(lookback_days=lookback)
            except Exception as exc:
                errors.append(f"ServiceNow change fetch failed: {exc}. Falling back to force_all.")
                changed_sys_ids = {a.sys_id for a in applications}

            _log(f"[{run_id}] {len(changed_sys_ids)} CIs have recent changes.")

        # ── 4. Scan + score ────────────────────────────────────────────────
        results: list[ScoreResult] = []
        scan_warnings: dict[str, list[str]] = {}
        apps_scanned = 0
        apps_skipped = 0

        for app in applications:
            if app.sys_id not in changed_sys_ids:
                apps_skipped += 1
                _log(f"[{run_id}]   SKIP  {app.app_name} (no recent changes)")
                continue

            _log(f"[{run_id}]   SCAN  {app.app_name} ({app.db_connection.get('type','?')} DB)…")
            apps_scanned += 1

            try:
                db_result = self.scanner.scan(app.db_connection)
            except Exception as exc:
                errors.append(f"DB scan failed for '{app.app_name}': {exc}")
                db_result = DBScanResult(
                    record_count=0, data_gb=0.0, oldest_record_age_days=0,
                    scanned_at=datetime.now(timezone.utc).isoformat(),
                    db_type="error",
                    warnings=[f"Scan error: {exc}"],
                )

            if db_result.warnings:
                scan_warnings[app.app_name] = db_result.warnings

            row = self._merge(app, db_result)
            score_result = compute_score(row, self.config)
            results.append(score_result)

            _log(
                f"[{run_id}]         → score={score_result.score:.1f}  "
                f"tier={score_result.tier}  "
                f"records={db_result.record_count:,}  "
                f"storage={db_result.data_gb:.1f}GB"
            )

        # ── 5. Sort results ────────────────────────────────────────────────
        results.sort(key=lambda r: r.score, reverse=True)

        # ── 6. Write back to ServiceNow ────────────────────────────────────
        write_cfg = self.config.get("servicenow", {}).get("write_back", {})
        if write_cfg.get("enabled", False):
            _log(f"[{run_id}] Writing {len(results)} scores back to ServiceNow…")
            for result in results:
                ok = self.sn.write_score(result)
                if not ok:
                    errors.append(f"Write-back failed for '{result.app_name}'")

        finished_at = datetime.now(timezone.utc).isoformat()
        summary = MonitorRunSummary(
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            apps_found=len(applications),
            apps_scanned=apps_scanned,
            apps_skipped=apps_skipped,
            results=results,
            scan_warnings=scan_warnings,
            config_issues=config_issues,
            errors=errors,
        )

        self._print_summary(summary)
        return summary

    # ── Helpers ────────────────────────────────────────────────────────────

    def _merge(self, app: ApplicationCI, db: DBScanResult) -> dict:
        """Combine CMDB metadata and DB scan result into a POP scoring row."""
        return {
            "app_name":               app.app_name,
            "business_function":      app.business_function,
            "has_ssn":                app.has_ssn,
            "has_financial":          app.has_financial,
            "has_cpni":               app.has_cpni,
            "record_count":           db.record_count,
            "data_amount_gb":         db.data_gb,
            "oldest_record_age_days": db.oldest_record_age_days,
            "retention_policy_days":  app.retention_policy_days,
        }

    def _print_summary(self, s: MonitorRunSummary) -> None:
        _log("")
        _log(f"── POP Monitor Run: {s.run_id} ──────────────────────────────")
        _log(f"   Started:   {s.started_at}")
        _log(f"   Finished:  {s.finished_at}")
        _log(f"   Apps found:   {s.apps_found}")
        _log(f"   Apps scanned: {s.apps_scanned}")
        _log(f"   Apps skipped: {s.apps_skipped} (no recent changes)")
        _log("")
        _log("   Tier breakdown:")
        for tier in ("Critical", "High", "Medium", "Exception"):
            n = s.tier_counts.get(tier, 0)
            if n:
                bar = "█" * n
                _log(f"     {tier:<10} {bar} {n}")
        if s.config_issues:
            _log("")
            _log("   Config warnings:")
            for issue in s.config_issues:
                _log(f"     ⚠  {issue}")
        if s.errors:
            _log("")
            _log("   Errors:")
            for err in s.errors:
                _log(f"     ✗  {err}")
        _log("")


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


# ── Output helpers ────────────────────────────────────────────────────────────

def write_csv(summary: MonitorRunSummary, path: str) -> None:
    """Write ranked results to a CSV file."""
    rows = []
    for r in summary.results:
        row = {
            "run_id":   summary.run_id,
            "app_name": r.app_name,
            "score":    f"{r.score:.2f}",
            "tier":     r.tier,
        }
        for fr in r.factors:
            row[f"factor_{fr.id}"]   = f"{fr.raw_score:.2f}"
            row[f"weighted_{fr.id}"] = f"{fr.weighted_score:.2f}"
        row["scan_warnings"] = "; ".join(summary.scan_warnings.get(r.app_name, []))
        row["score_warnings"] = "; ".join(r.warnings)
        rows.append(row)

    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_json_summary(summary: MonitorRunSummary, path: str) -> None:
    """Write a machine-readable run summary for audit/dashboards."""
    data = {
        "run_id":       summary.run_id,
        "started_at":   summary.started_at,
        "finished_at":  summary.finished_at,
        "apps_found":   summary.apps_found,
        "apps_scanned": summary.apps_scanned,
        "apps_skipped": summary.apps_skipped,
        "tier_counts":  summary.tier_counts,
        "config_issues": summary.config_issues,
        "errors":        summary.errors,
        "results": [
            {
                "app_name": r.app_name,
                "score":    r.score,
                "tier":     r.tier,
                "factors":  {fr.id: {"raw": fr.raw_score, "weighted": fr.weighted_score}
                             for fr in r.factors},
                "warnings": r.warnings,
            }
            for r in summary.results
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
