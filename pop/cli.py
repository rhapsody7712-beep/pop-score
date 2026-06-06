"""
pop.cli
~~~~~~~
Command-line interface for the POP scoring engine.

Usage:
    python -m pop.cli score apps.csv
    python -m pop.cli score apps.csv --config config.yaml --output results.csv
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path


def _load_config(path: str) -> dict:
    try:
        import yaml
    except ImportError:
        print("ERROR: PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f)


def _read_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_csv(rows: list[dict], path: str | None) -> None:
    if not rows:
        return
    out = open(path, "w", newline="", encoding="utf-8") if path else sys.stdout
    try:
        writer = csv.DictWriter(out, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    finally:
        if path:
            out.close()


def _result_to_row(result) -> dict:
    """Flatten a ScoreResult into a CSV-friendly dict."""
    row = {
        "app_name": result.app_name,
        "score":    f"{result.score:.2f}",
        "tier":     result.tier,
    }
    # One factor_* column (0-100) and one weighted_* column per factor
    for fr in result.factors:
        row[f"factor_{fr.id}"]   = f"{fr.raw_score:.2f}"
        row[f"weighted_{fr.id}"] = f"{fr.weighted_score:.2f}"
    row["warnings"] = "; ".join(result.warnings)
    return row


def cmd_score(args: list[str]) -> None:
    """score <input.csv> [--config config.yaml] [--output results.csv]"""
    if not args:
        print("Usage: python -m pop.cli score <apps.csv> [--config config.yaml] [--output results.csv]")
        sys.exit(1)

    input_csv  = args[0]
    config_path = "config.yaml"
    output_csv: str | None = None

    i = 1
    while i < len(args):
        if args[i] == "--config" and i + 1 < len(args):
            config_path = args[i + 1]; i += 2
        elif args[i] == "--output" and i + 1 < len(args):
            output_csv = args[i + 1]; i += 2
        else:
            print(f"Unknown argument: {args[i]}", file=sys.stderr)
            sys.exit(1)

    # Resolve config path
    if not Path(config_path).exists():
        alt = Path(input_csv).parent / config_path
        if alt.exists():
            config_path = str(alt)
        else:
            print(f"ERROR: config file not found: {config_path}", file=sys.stderr)
            sys.exit(1)

    from pop.scorer import compute_score, validate_config

    config = _load_config(config_path)

    # Validate config and surface any issues before scoring
    config_issues = validate_config(config)
    for issue in config_issues:
        print(f"CONFIG WARNING: {issue}", file=sys.stderr)

    rows = _read_csv(input_csv)
    if not rows:
        print("No rows found in input CSV.", file=sys.stderr)
        sys.exit(1)

    results = [compute_score(row, config) for row in rows]
    results.sort(key=lambda r: r.score, reverse=True)

    output_rows = [_result_to_row(r) for r in results]
    _write_csv(output_rows, output_csv)

    # Summary to stderr
    tier_counts: dict[str, int] = {}
    for r in results:
        tier_counts[r.tier] = tier_counts.get(r.tier, 0) + 1

    dest = output_csv or "stdout"
    print(f"\nScored {len(results)} applications → {dest}", file=sys.stderr)
    for tier in ("Critical", "High", "Medium", "Exception"):
        n = tier_counts.get(tier, 0)
        if n:
            print(f"  {tier}: {n}", file=sys.stderr)

    warnings_total = sum(len(r.warnings) for r in results)
    if warnings_total:
        print(f"\n  {warnings_total} data warning(s) — see 'warnings' column.", file=sys.stderr)

    enabled = [f for f in config.get("factors", []) if f.get("enabled", True)]
    print(f"\n  Active factors: {', '.join(f['id'] for f in enabled)}", file=sys.stderr)


def cmd_monitor(args: list[str]) -> None:
    """
    monitor [--config config.yaml] [--output-dir .] [--force-all] [--dry-run]

    Run one full POP monitor cycle:
      1. Pull application CIs from ServiceNow
      2. Detect recently changed/deployed apps
      3. Initiate a DB metadata scan for each changed app
      4. Score all scanned apps
      5. Write results back to ServiceNow (if write_back.enabled = true)
      6. Emit a ranked CSV + JSON run-summary
    """
    config_path = "config.yaml"
    output_dir  = "."
    force_all   = False
    dry_run     = False

    i = 0
    while i < len(args):
        if args[i] == "--config" and i + 1 < len(args):
            config_path = args[i + 1]; i += 2
        elif args[i] == "--output-dir" and i + 1 < len(args):
            output_dir = args[i + 1]; i += 2
        elif args[i] == "--force-all":
            force_all = True; i += 1
        elif args[i] == "--dry-run":
            dry_run = True; i += 1
        else:
            print(f"Unknown argument: {args[i]}", file=sys.stderr); sys.exit(1)

    if not Path(config_path).exists():
        print(f"ERROR: config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    from pop.monitor import POPMonitor, write_csv, write_json_summary

    config = _load_config(config_path)
    if dry_run:
        # Force mock mode for dry run
        config.setdefault("servicenow", {})["mock_mode"] = True

    monitor = POPMonitor(config)
    summary = monitor.run(force_all=force_all)

    # Write outputs
    import os
    os.makedirs(output_dir, exist_ok=True)
    csv_path  = Path(output_dir) / f"{summary.run_id}.csv"
    json_path = Path(output_dir) / f"{summary.run_id}.json"

    write_csv(summary, str(csv_path))
    write_json_summary(summary, str(json_path))

    print(f"\nOutputs written:", file=sys.stderr)
    print(f"  CSV:  {csv_path}", file=sys.stderr)
    print(f"  JSON: {json_path}", file=sys.stderr)

    if summary.errors:
        sys.exit(1)


def main() -> None:
    argv = sys.argv[1:]
    if not argv:
        print("Usage: python -m pop.cli <command> [args]")
        print("Commands: score | monitor")
        sys.exit(0)

    command, *rest = argv
    if command == "score":
        cmd_score(rest)
    elif command == "monitor":
        cmd_monitor(rest)
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
