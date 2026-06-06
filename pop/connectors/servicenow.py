"""
pop.connectors.servicenow
~~~~~~~~~~~~~~~~~~~~~~~~~
ServiceNow REST API client for the POP monitor.

Responsibilities:
  - Pull application CIs from the CMDB (cmdb_ci_appl or configured table)
  - Pull recently changed/deployed CIs (change_request + deployment records)
  - Write POP scores back to a ServiceNow custom table for audit/dashboards

Authentication: HTTP Basic (username/password).  Credentials are read from
environment variables — never hardcode them in config.yaml.

Field mappings between ServiceNow CI fields and POP model fields are fully
configured in config.yaml under servicenow.field_mappings, so no code changes
are needed when field names differ across ServiceNow instances.

Mock mode (servicenow.mock_mode: true) returns synthetic CIs so the full
pipeline can be demoed without a live ServiceNow instance.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.request
import urllib.parse
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class ApplicationCI:
    """A ServiceNow CMDB Application CI with fields mapped to POP model inputs."""
    sys_id: str
    app_name: str
    business_function: str
    has_ssn: bool
    has_financial: bool
    has_cpni: bool
    retention_policy_days: int
    db_connection: dict       # passed to DBScanner — type, host, port, db name, credentials key
    raw: dict = field(default_factory=dict)   # full raw CI for audit


@dataclass
class ChangeRecord:
    """A ServiceNow change/deployment that affected an application CI."""
    sys_id: str
    affected_ci_sys_id: str
    change_type: str          # deployment | db_update | config_change
    updated_on: datetime
    description: str


# ── Client ────────────────────────────────────────────────────────────────────

class ServiceNowClient:
    """
    Thin wrapper around the ServiceNow Table REST API.

    All table names and field names come from config so this class never
    needs editing when your ServiceNow instance uses different naming.
    """

    def __init__(self, config: dict[str, Any]):
        self.cfg = config
        self.instance = config["instance"].rstrip("/")
        self.mock_mode = config.get("mock_mode", False)

        if not self.mock_mode:
            username = self._resolve_env(config.get("username", ""))
            password = self._resolve_env(config.get("password", ""))
            credentials = f"{username}:{password}"
            self._auth_header = "Basic " + base64.b64encode(credentials.encode()).decode()

    # ── Public API ─────────────────────────────────────────────────────────

    def get_applications(self) -> list[ApplicationCI]:
        """
        Return all active application CIs from the CMDB.
        In mock mode returns a synthetic set matching apps.csv.
        """
        if self.mock_mode:
            return self._mock_applications()

        table = self.cfg.get("cmdb_table", "cmdb_ci_appl")
        fm = self.cfg.get("field_mappings", {})
        fields = list(fm.values()) + ["sys_id", "sys_updated_on"]

        params = {
            "sysparm_fields": ",".join(fields),
            "sysparm_query":  "operational_status=1",   # active CIs only
            "sysparm_limit":  str(self.cfg.get("max_records", 1000)),
        }
        records = self._get(table, params)
        return [self._map_ci(r) for r in records]

    def get_recent_changes(self, lookback_days: int = 35) -> set[str]:
        """
        Return a set of CI sys_ids that have had a change/deployment record
        in the last ``lookback_days`` days.  Used to decide which apps need
        a fresh DB scan this cycle.
        """
        if self.mock_mode:
            # In mock mode, flag every other app as recently changed
            apps = self._mock_applications()
            return {a.sys_id for i, a in enumerate(apps) if i % 2 == 0}

        change_table = self.cfg.get("change_table", "change_request")
        since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        params = {
            "sysparm_fields": "cmdb_ci,sys_updated_on,type,short_description",
            "sysparm_query":  f"sys_updated_on>={since}^state=3",   # state=3 = Closed/Implemented
            "sysparm_limit":  "5000",
        }
        records = self._get(change_table, params)
        return {r.get("cmdb_ci", {}).get("value", "") for r in records if r.get("cmdb_ci")}

    def write_score(self, result, dry_run: bool = False) -> bool:
        """
        Write a POP ScoreResult back to ServiceNow.
        Creates/updates a record in the configured write-back table.
        Returns True on success.
        """
        write_cfg = self.cfg.get("write_back", {})
        if not write_cfg.get("enabled", False):
            return False
        if dry_run or self.mock_mode:
            return True   # mock: pretend it worked

        table = write_cfg.get("table", "u_pop_score_result")
        payload = {
            "u_app_name":    result.app_name,
            "u_score":       str(result.score),
            "u_tier":        result.tier,
            "u_scored_at":   datetime.now(timezone.utc).isoformat(),
            "u_factor_json": json.dumps(
                {fr.id: fr.raw_score for fr in result.factors}
            ),
            "u_warnings":    "; ".join(result.warnings),
        }
        try:
            self._post(table, payload)
            return True
        except Exception as exc:
            return False

    # ── HTTP helpers ────────────────────────────────────────────────────────

    def _get(self, table: str, params: dict) -> list[dict]:
        url = f"{self.instance}/api/now/table/{table}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", self._auth_header)
        req.add_header("Accept", "application/json")
        timeout = self.cfg.get("timeout_seconds", 30)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode()).get("result", [])
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"ServiceNow GET {table} failed: HTTP {e.code}") from e

    def _post(self, table: str, payload: dict) -> dict:
        url = f"{self.instance}/api/now/table/{table}"
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Authorization", self._auth_header)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        timeout = self.cfg.get("timeout_seconds", 30)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode()).get("result", {})

    # ── Field mapping ───────────────────────────────────────────────────────

    def _map_ci(self, raw: dict) -> ApplicationCI:
        """Map a raw ServiceNow CI dict to ApplicationCI using config field_mappings."""
        fm = self.cfg.get("field_mappings", {})

        def get(pop_field: str, default: Any = "") -> Any:
            sn_field = fm.get(pop_field, pop_field)
            val = raw.get(sn_field, default)
            # ServiceNow reference fields come as {"value": ..., "display_value": ...}
            if isinstance(val, dict):
                val = val.get("display_value", val.get("value", default))
            return val

        def get_bool(pop_field: str) -> bool:
            val = get(pop_field, "false")
            return str(val).strip().lower() in {"true", "1", "yes"}

        # DB connection info — stored in a separate CMDB rel table in production;
        # here we read from a flat field u_db_connection_json if present.
        db_raw = get("db_connection_json", "{}")
        try:
            db_connection = json.loads(db_raw) if isinstance(db_raw, str) else db_raw
        except (json.JSONDecodeError, TypeError):
            db_connection = {}

        return ApplicationCI(
            sys_id=raw.get("sys_id", ""),
            app_name=get("app_name", "unknown"),
            business_function=get("business_function", "backend"),
            has_ssn=get_bool("has_ssn"),
            has_financial=get_bool("has_financial"),
            has_cpni=get_bool("has_cpni"),
            retention_policy_days=int(get("retention_policy_days") or 1095),
            db_connection=db_connection,
            raw=raw,
        )

    # ── Mock data ───────────────────────────────────────────────────────────

    def _mock_applications(self) -> list[ApplicationCI]:
        """Synthetic CIs that mirror apps.csv — used when mock_mode=true."""
        mock_apps = [
            ("sn-001", "Salesforce CRM",               "consumer",  True,  False, False, 1825),
            ("sn-002", "Payment Gateway",               "backend",   True,  True,  False, 365),
            ("sn-003", "Customer Mobile App",           "consumer",  False, False, True,  730),
            ("sn-004", "Marketing Analytics Platform",  "marketing", False, False, False, 365),
            ("sn-005", "Internal HR System",            "internal",  True,  False, False, 2190),
            ("sn-006", "Employee Expense Tool",         "internal",  False, True,  False, 1095),
            ("sn-007", "Data Warehouse",                "backend",   True,  True,  True,  1825),
            ("sn-008", "Email Campaign Manager",        "marketing", False, False, False, 365),
            ("sn-009", "Customer Support Portal",       "consumer",  False, False, False, 1095),
            ("sn-010", "IT Asset Registry",             "internal",  False, False, False, 2190),
        ]
        return [
            ApplicationCI(
                sys_id=sys_id,
                app_name=name,
                business_function=fn,
                has_ssn=ssn,
                has_financial=fin,
                has_cpni=cpni,
                retention_policy_days=ret,
                db_connection={"type": "mock", "app_id": sys_id},
            )
            for sys_id, name, fn, ssn, fin, cpni, ret in mock_apps
        ]

    @staticmethod
    def _resolve_env(value: str) -> str:
        """Resolve ${ENV_VAR} patterns from environment variables."""
        if value.startswith("${") and value.endswith("}"):
            var_name = value[2:-1]
            resolved = os.environ.get(var_name, "")
            if not resolved:
                raise EnvironmentError(
                    f"ServiceNow credential env var '{var_name}' is not set. "
                    "Set it before running the monitor."
                )
            return resolved
        return value
