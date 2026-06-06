"""
Tests for pop.connectors and pop.monitor.
All tests run in mock mode — no real ServiceNow or database connection required.
"""

import json
import os
import tempfile
import pytest

from pop.connectors.servicenow import ServiceNowClient, ApplicationCI
from pop.connectors.db_scanner import DBScanner, DBScanResult, _scan_mock
from pop.monitor import POPMonitor, write_csv, write_json_summary


# ── Config fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def base_scoring_cfg():
    return {
        "factors": [
            {
                "id": "business_function", "label": "Business Function",
                "enabled": True, "weight": 0.25, "type": "lookup",
                "column": "business_function",
                "lookup": {"values": {"consumer": 100, "marketing": 100, "internal": 40, "backend": 20}, "default": 20},
            },
            {
                "id": "data_sensitivity", "label": "Data Sensitivity",
                "enabled": True, "weight": 0.25, "type": "max_flag",
                "flags": {"has_ssn": 100, "has_financial": 85, "has_cpni": 70},
                "default": 10,
            },
            {
                "id": "data_volume", "label": "Data Volume",
                "enabled": True, "weight": 0.20, "type": "log_scale",
                "column": "record_count", "reference_max": 10_000_000,
            },
            {
                "id": "data_amount_gb", "label": "Storage",
                "enabled": True, "weight": 0.15, "type": "log_scale",
                "column": "data_amount_gb", "reference_max": 10_000,
            },
            {
                "id": "retention_risk", "label": "Retention Risk",
                "enabled": True, "weight": 0.15, "type": "ratio",
                "numerator_column": "oldest_record_age_days",
                "denominator_column": "retention_policy_days",
                "no_denominator_score": 100,
            },
        ],
        "tiers": {"critical": 80, "high": 60, "medium": 40},
    }


@pytest.fixture
def mock_cfg(base_scoring_cfg):
    return {
        **base_scoring_cfg,
        "servicenow": {
            "instance":    "https://demo.service-now.com",
            "mock_mode":   True,
            "write_back":  {"enabled": False},
            "field_mappings": {},
        },
        "db_scanner": {"timeout_seconds": 5, "date_columns": []},
        "monitor":    {"lookback_days": 35, "output_dir": "."},
    }


# ── ServiceNowClient (mock mode) ──────────────────────────────────────────────

class TestServiceNowClientMock:
    def test_get_applications_returns_list(self, mock_cfg):
        client = ServiceNowClient(mock_cfg["servicenow"])
        apps = client.get_applications()
        assert isinstance(apps, list)
        assert len(apps) > 0

    def test_all_results_are_application_cis(self, mock_cfg):
        client = ServiceNowClient(mock_cfg["servicenow"])
        for app in client.get_applications():
            assert isinstance(app, ApplicationCI)

    def test_app_has_required_fields(self, mock_cfg):
        client = ServiceNowClient(mock_cfg["servicenow"])
        app = client.get_applications()[0]
        assert app.sys_id
        assert app.app_name
        assert app.business_function in {"consumer", "marketing", "internal", "backend"}
        assert isinstance(app.has_ssn, bool)
        assert isinstance(app.retention_policy_days, int)
        assert app.retention_policy_days > 0

    def test_get_recent_changes_returns_set_of_sys_ids(self, mock_cfg):
        client = ServiceNowClient(mock_cfg["servicenow"])
        changed = client.get_recent_changes()
        assert isinstance(changed, set)

    def test_recent_changes_subset_of_all_apps(self, mock_cfg):
        client = ServiceNowClient(mock_cfg["servicenow"])
        all_ids = {a.sys_id for a in client.get_applications()}
        changed = client.get_recent_changes()
        assert changed.issubset(all_ids)

    def test_write_score_mock_returns_false_when_disabled(self, mock_cfg):
        client = ServiceNowClient(mock_cfg["servicenow"])
        from pop.scorer import compute_score, ScoreResult
        # write_back.enabled = False → should return False
        mock_result = ScoreResult(app_name="Test", score=75.0, tier="High", factors=[], warnings=[])
        assert client.write_score(mock_result) is False

    def test_write_score_mock_returns_true_when_enabled(self, mock_cfg):
        cfg = {**mock_cfg["servicenow"], "write_back": {"enabled": True}}
        client = ServiceNowClient(cfg)
        from pop.scorer import ScoreResult
        result = ScoreResult(app_name="Test", score=75.0, tier="High", factors=[], warnings=[])
        assert client.write_score(result) is True   # mock mode always succeeds


# ── ServiceNowClient env var resolution ──────────────────────────────────────

class TestEnvResolution:
    def test_resolves_env_var(self):
        os.environ["_POP_TEST_VAR"] = "test_value"
        try:
            result = ServiceNowClient._resolve_env("${_POP_TEST_VAR}")
            assert result == "test_value"
        finally:
            del os.environ["_POP_TEST_VAR"]

    def test_plain_string_unchanged(self):
        assert ServiceNowClient._resolve_env("plaintext") == "plaintext"

    def test_missing_env_var_raises(self):
        os.environ.pop("_POP_MISSING_VAR", None)
        with pytest.raises(EnvironmentError, match="_POP_MISSING_VAR"):
            ServiceNowClient._resolve_env("${_POP_MISSING_VAR}")


# ── DBScanner (mock) ──────────────────────────────────────────────────────────

class TestDBScannerMock:
    def test_mock_scan_returns_result(self):
        scanner = DBScanner({})
        result = scanner.scan({"type": "mock", "app_id": "test-001"})
        assert isinstance(result, DBScanResult)

    def test_mock_result_has_positive_values(self):
        result = _scan_mock({"app_id": "sn-001"}, {})
        assert result.record_count > 0
        assert result.data_gb > 0
        assert result.oldest_record_age_days > 0

    def test_mock_is_deterministic(self):
        r1 = _scan_mock({"app_id": "sn-007"}, {})
        r2 = _scan_mock({"app_id": "sn-007"}, {})
        assert r1.record_count == r2.record_count
        assert r1.data_gb == r2.data_gb

    def test_different_apps_get_different_values(self):
        r1 = _scan_mock({"app_id": "sn-001"}, {})
        r2 = _scan_mock({"app_id": "sn-007"}, {})
        assert r1.record_count != r2.record_count

    def test_unknown_db_type_returns_warnings(self):
        scanner = DBScanner({})
        result = scanner.scan({"type": "oracle_rac"})
        assert result.record_count == 0
        assert any("oracle_rac" in w for w in result.warnings)

    def test_missing_type_defaults_to_mock(self):
        scanner = DBScanner({})
        result = scanner.scan({"app_id": "fallback-test"})
        assert result.db_type == "mock"
        assert result.record_count > 0


# ── POPMonitor (mock, full cycle) ─────────────────────────────────────────────

class TestPOPMonitorMock:
    def test_run_returns_summary(self, mock_cfg):
        monitor = POPMonitor(mock_cfg)
        summary = monitor.run()
        assert summary.run_id.startswith("pop-run-")
        assert summary.apps_found > 0

    def test_scanned_plus_skipped_equals_found(self, mock_cfg):
        monitor = POPMonitor(mock_cfg)
        summary = monitor.run()
        assert summary.apps_scanned + summary.apps_skipped == summary.apps_found

    def test_results_are_scored(self, mock_cfg):
        monitor = POPMonitor(mock_cfg)
        summary = monitor.run()
        for r in summary.results:
            assert 0 <= r.score <= 100
            assert r.tier in {"Critical", "High", "Medium", "Exception"}

    def test_results_sorted_by_score_descending(self, mock_cfg):
        monitor = POPMonitor(mock_cfg)
        summary = monitor.run()
        scores = [r.score for r in summary.results]
        assert scores == sorted(scores, reverse=True)

    def test_force_all_scans_all_apps(self, mock_cfg):
        monitor = POPMonitor(mock_cfg)
        summary = monitor.run(force_all=True)
        assert summary.apps_scanned == summary.apps_found
        assert summary.apps_skipped == 0

    def test_no_errors_in_mock_run(self, mock_cfg):
        monitor = POPMonitor(mock_cfg)
        summary = monitor.run()
        assert summary.errors == []

    def test_tier_counts_sum_to_results(self, mock_cfg):
        monitor = POPMonitor(mock_cfg)
        summary = monitor.run(force_all=True)
        assert sum(summary.tier_counts.values()) == len(summary.results)


# ── Output writers ────────────────────────────────────────────────────────────

class TestOutputWriters:
    def _run(self, mock_cfg):
        monitor = POPMonitor(mock_cfg)
        return monitor.run(force_all=True)

    def test_write_csv_creates_file(self, mock_cfg, tmp_path):
        summary = self._run(mock_cfg)
        csv_path = str(tmp_path / "out.csv")
        write_csv(summary, csv_path)
        assert os.path.exists(csv_path)

    def test_csv_has_expected_columns(self, mock_cfg, tmp_path):
        import csv as csv_mod
        summary = self._run(mock_cfg)
        csv_path = str(tmp_path / "out.csv")
        write_csv(summary, csv_path)
        rows = list(csv_mod.DictReader(open(csv_path)))
        assert rows
        assert "score" in rows[0]
        assert "tier" in rows[0]
        assert "run_id" in rows[0]

    def test_csv_row_count_matches_results(self, mock_cfg, tmp_path):
        import csv as csv_mod
        summary = self._run(mock_cfg)
        csv_path = str(tmp_path / "out.csv")
        write_csv(summary, csv_path)
        rows = list(csv_mod.DictReader(open(csv_path)))
        assert len(rows) == len(summary.results)

    def test_write_json_summary_creates_file(self, mock_cfg, tmp_path):
        summary = self._run(mock_cfg)
        json_path = str(tmp_path / "run.json")
        write_json_summary(summary, json_path)
        assert os.path.exists(json_path)

    def test_json_summary_is_valid(self, mock_cfg, tmp_path):
        summary = self._run(mock_cfg)
        json_path = str(tmp_path / "run.json")
        write_json_summary(summary, json_path)
        data = json.loads(open(json_path).read())
        assert data["run_id"] == summary.run_id
        assert "tier_counts" in data
        assert "results" in data
        assert len(data["results"]) == len(summary.results)

    def test_json_results_have_factor_breakdown(self, mock_cfg, tmp_path):
        summary = self._run(mock_cfg)
        json_path = str(tmp_path / "run.json")
        write_json_summary(summary, json_path)
        data = json.loads(open(json_path).read())
        for r in data["results"]:
            assert "factors" in r
            assert len(r["factors"]) > 0
