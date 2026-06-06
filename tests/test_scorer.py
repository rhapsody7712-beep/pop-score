"""
Unit tests for pop.scorer (config-driven engine).

Tests cover:
- Each factor type at boundary and typical values
- Tier assignment at and around each threshold
- Composite score correctness
- Config validation
- Edge cases: missing columns, unknown values, zero denominators
"""

import math
import pytest

from pop.scorer import (
    _score_lookup,
    _score_log_scale,
    _score_max_flag,
    _score_ratio,
    _score_linear_scale,
    assign_tier,
    compute_score,
    validate_config,
)


# ── Config fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def cfg():
    """Minimal but complete config matching config.yaml structure."""
    return {
        "factors": [
            {
                "id": "business_function",
                "label": "Business Function",
                "enabled": True,
                "weight": 0.25,
                "type": "lookup",
                "column": "business_function",
                "lookup": {
                    "values": {
                        "consumer": 100,
                        "marketing": 100,
                        "internal": 40,
                        "backend": 20,
                    },
                    "default": 20,
                },
            },
            {
                "id": "data_sensitivity",
                "label": "Data Sensitivity",
                "enabled": True,
                "weight": 0.25,
                "type": "max_flag",
                "flags": {
                    "has_ssn": 100,
                    "has_financial": 85,
                    "has_cpni": 70,
                },
                "default": 10,
            },
            {
                "id": "data_volume",
                "label": "Data Volume",
                "enabled": True,
                "weight": 0.20,
                "type": "log_scale",
                "column": "record_count",
                "reference_max": 10_000_000,
            },
            {
                "id": "data_amount_gb",
                "label": "Data Footprint",
                "enabled": True,
                "weight": 0.15,
                "type": "log_scale",
                "column": "data_amount_gb",
                "reference_max": 10_000,
            },
            {
                "id": "retention_risk",
                "label": "Retention Risk",
                "enabled": True,
                "weight": 0.15,
                "type": "ratio",
                "numerator_column": "oldest_record_age_days",
                "denominator_column": "retention_policy_days",
                "no_denominator_score": 100,
            },
        ],
        "tiers": {"critical": 80, "high": 60, "medium": 40},
    }


@pytest.fixture
def lookup_factor(cfg):
    return cfg["factors"][0]

@pytest.fixture
def flag_factor(cfg):
    return cfg["factors"][1]

@pytest.fixture
def volume_factor(cfg):
    return cfg["factors"][2]

@pytest.fixture
def storage_factor(cfg):
    return cfg["factors"][3]

@pytest.fixture
def retention_factor(cfg):
    return cfg["factors"][4]


# ── lookup factor ─────────────────────────────────────────────────────────────

class TestLookup:
    def test_consumer_100(self, lookup_factor):
        assert _score_lookup(lookup_factor, {"business_function": "consumer"}, []) == 100.0

    def test_marketing_100(self, lookup_factor):
        assert _score_lookup(lookup_factor, {"business_function": "marketing"}, []) == 100.0

    def test_internal_40(self, lookup_factor):
        assert _score_lookup(lookup_factor, {"business_function": "internal"}, []) == 40.0

    def test_backend_20(self, lookup_factor):
        assert _score_lookup(lookup_factor, {"business_function": "backend"}, []) == 20.0

    def test_case_insensitive(self, lookup_factor):
        assert _score_lookup(lookup_factor, {"business_function": "CONSUMER"}, []) == 100.0

    def test_whitespace_stripped(self, lookup_factor):
        assert _score_lookup(lookup_factor, {"business_function": "  internal  "}, []) == 40.0

    def test_unknown_uses_default_and_warns(self, lookup_factor):
        warnings = []
        result = _score_lookup(lookup_factor, {"business_function": "partner"}, warnings)
        assert result == 20.0
        assert len(warnings) == 1
        assert "partner" in warnings[0]

    def test_missing_column_uses_default(self, lookup_factor):
        warnings = []
        result = _score_lookup(lookup_factor, {}, warnings)
        assert result == 20.0


# ── max_flag factor ───────────────────────────────────────────────────────────

class TestMaxFlag:
    def test_no_flags_returns_default(self, flag_factor):
        row = {"has_ssn": False, "has_financial": False, "has_cpni": False}
        assert _score_max_flag(flag_factor, row, []) == 10.0

    def test_ssn_only(self, flag_factor):
        assert _score_max_flag(flag_factor, {"has_ssn": True}, []) == 100.0

    def test_financial_only(self, flag_factor):
        assert _score_max_flag(flag_factor, {"has_financial": True}, []) == 85.0

    def test_cpni_only(self, flag_factor):
        assert _score_max_flag(flag_factor, {"has_cpni": True}, []) == 70.0

    def test_multiple_flags_returns_max(self, flag_factor):
        row = {"has_ssn": True, "has_financial": True, "has_cpni": True}
        assert _score_max_flag(flag_factor, row, []) == 100.0

    def test_financial_and_cpni_returns_85(self, flag_factor):
        row = {"has_ssn": False, "has_financial": True, "has_cpni": True}
        assert _score_max_flag(flag_factor, row, []) == 85.0

    def test_string_truthy_values(self, flag_factor):
        for val in ("true", "True", "1", "yes", "YES"):
            assert _score_max_flag(flag_factor, {"has_ssn": val}, []) == 100.0

    def test_empty_row_returns_default(self, flag_factor):
        assert _score_max_flag(flag_factor, {}, []) == 10.0


# ── log_scale factor ──────────────────────────────────────────────────────────

class TestLogScale:
    def test_zero_returns_0(self, volume_factor):
        assert _score_log_scale(volume_factor, {"record_count": 0}, []) == 0.0

    def test_negative_returns_0(self, volume_factor):
        assert _score_log_scale(volume_factor, {"record_count": -500}, []) == 0.0

    def test_at_reference_max_scores_100(self, volume_factor):
        assert _score_log_scale(volume_factor, {"record_count": 10_000_000}, []) == pytest.approx(100.0)

    def test_above_reference_capped_at_100(self, volume_factor):
        assert _score_log_scale(volume_factor, {"record_count": 99_000_000}, []) == 100.0

    def test_log_midpoint_scores_50(self, volume_factor):
        # log10(sqrt(10M)) = log10(10M)/2 → score = 50
        mid = math.sqrt(10_000_000)
        assert _score_log_scale(volume_factor, {"record_count": mid}, []) == pytest.approx(50.0, abs=0.01)

    def test_1m_records(self, volume_factor):
        # log10(1M)/log10(10M) = 6/7
        result = _score_log_scale(volume_factor, {"record_count": 1_000_000}, [])
        assert result == pytest.approx(100 * 6 / 7, abs=0.1)

    def test_storage_factor_at_ceiling(self, storage_factor):
        assert _score_log_scale(storage_factor, {"data_amount_gb": 10_000}, []) == pytest.approx(100.0)

    def test_bad_value_warns_and_returns_0(self, volume_factor):
        warnings = []
        result = _score_log_scale(volume_factor, {"record_count": "many"}, warnings)
        assert result == 0.0
        assert len(warnings) == 1


# ── ratio factor ──────────────────────────────────────────────────────────────

class TestRatio:
    def test_zero_denominator_returns_no_denominator_score(self, retention_factor):
        assert _score_ratio(retention_factor, {"oldest_record_age_days": 365, "retention_policy_days": 0}, []) == 100.0

    def test_missing_denominator_returns_no_denominator_score(self, retention_factor):
        assert _score_ratio(retention_factor, {"oldest_record_age_days": 365}, []) == 100.0

    def test_at_boundary_scores_100(self, retention_factor):
        row = {"oldest_record_age_days": 365, "retention_policy_days": 365}
        assert _score_ratio(retention_factor, row, []) == pytest.approx(100.0)

    def test_exceeds_boundary_capped_at_100(self, retention_factor):
        row = {"oldest_record_age_days": 730, "retention_policy_days": 365}
        assert _score_ratio(retention_factor, row, []) == 100.0

    def test_halfway_scores_50(self, retention_factor):
        row = {"oldest_record_age_days": 365, "retention_policy_days": 730}
        assert _score_ratio(retention_factor, row, []) == pytest.approx(50.0)

    def test_zero_numerator_scores_0(self, retention_factor):
        row = {"oldest_record_age_days": 0, "retention_policy_days": 365}
        assert _score_ratio(retention_factor, row, []) == pytest.approx(0.0)

    def test_quarter_scores_25(self, retention_factor):
        row = {"oldest_record_age_days": 90, "retention_policy_days": 360}
        assert _score_ratio(retention_factor, row, []) == pytest.approx(25.0)


# ── linear_scale factor ───────────────────────────────────────────────────────

class TestLinearScale:
    @pytest.fixture
    def linear_factor(self):
        return {
            "id": "years_exp",
            "type": "linear_scale",
            "column": "years_experience",
            "min": 0,
            "max": 20,
        }

    def test_at_min_scores_0(self, linear_factor):
        assert _score_linear_scale(linear_factor, {"years_experience": 0}, []) == pytest.approx(0.0)

    def test_at_max_scores_100(self, linear_factor):
        assert _score_linear_scale(linear_factor, {"years_experience": 20}, []) == pytest.approx(100.0)

    def test_midpoint_scores_50(self, linear_factor):
        assert _score_linear_scale(linear_factor, {"years_experience": 10}, []) == pytest.approx(50.0)

    def test_above_max_clamped_to_100(self, linear_factor):
        assert _score_linear_scale(linear_factor, {"years_experience": 50}, []) == pytest.approx(100.0)

    def test_below_min_clamped_to_0(self, linear_factor):
        assert _score_linear_scale(linear_factor, {"years_experience": -5}, []) == pytest.approx(0.0)

    def test_invalid_max_warns(self, linear_factor):
        bad = {**linear_factor, "min": 10, "max": 5}
        warnings = []
        assert _score_linear_scale(bad, {"years_experience": 7}, warnings) == 0.0
        assert warnings


# ── assign_tier ───────────────────────────────────────────────────────────────

class TestTierBoundaries:
    def test_exactly_80_is_critical(self, cfg):
        assert assign_tier(80.0, cfg) == "Critical"

    def test_79_99_is_high(self, cfg):
        assert assign_tier(79.99, cfg) == "High"

    def test_exactly_60_is_high(self, cfg):
        assert assign_tier(60.0, cfg) == "High"

    def test_59_99_is_medium(self, cfg):
        assert assign_tier(59.99, cfg) == "Medium"

    def test_exactly_40_is_medium(self, cfg):
        assert assign_tier(40.0, cfg) == "Medium"

    def test_39_99_is_exception(self, cfg):
        assert assign_tier(39.99, cfg) == "Exception"

    def test_zero_is_exception(self, cfg):
        assert assign_tier(0.0, cfg) == "Exception"

    def test_100_is_critical(self, cfg):
        assert assign_tier(100.0, cfg) == "Critical"


# ── validate_config ───────────────────────────────────────────────────────────

class TestValidateConfig:
    def test_valid_config_no_warnings(self, cfg):
        assert validate_config(cfg) == []

    def test_no_factors_warns(self, cfg):
        bad = {**cfg, "factors": []}
        issues = validate_config(bad)
        assert any("No enabled factors" in i for i in issues)

    def test_all_disabled_warns(self, cfg):
        bad = {**cfg, "factors": [{**f, "enabled": False} for f in cfg["factors"]]}
        issues = validate_config(bad)
        assert any("No enabled factors" in i for i in issues)

    def test_weights_not_summing_warns(self, cfg):
        factors = [{**f, "weight": 0.1} for f in cfg["factors"]]
        issues = validate_config({**cfg, "factors": factors})
        assert any("sum" in i for i in issues)

    def test_unknown_factor_type_warns(self, cfg):
        factors = [{**cfg["factors"][0], "type": "neural_net"}] + cfg["factors"][1:]
        issues = validate_config({**cfg, "factors": factors})
        assert any("neural_net" in i for i in issues)

    def test_missing_tier_warns(self, cfg):
        bad_tiers = {"critical": 80, "high": 60}  # missing "medium"
        issues = validate_config({**cfg, "tiers": bad_tiers})
        assert any("medium" in i for i in issues)

    def test_disabled_factor_excluded_from_weight_sum(self, cfg):
        # Disable one 0.15-weight factor → remaining sum = 0.85 → should warn
        factors = [
            {**f, "enabled": False} if f["id"] == "retention_risk" else f
            for f in cfg["factors"]
        ]
        issues = validate_config({**cfg, "factors": factors})
        assert any("sum" in i for i in issues)


# ── compute_score (integration) ───────────────────────────────────────────────

class TestComputeScore:
    def _app(self, **overrides) -> dict:
        base = {
            "app_name": "Test App",
            "business_function": "consumer",
            "has_ssn": True,
            "has_financial": False,
            "has_cpni": False,
            "record_count": 1_000_000,
            "data_amount_gb": 500,
            "oldest_record_age_days": 730,
            "retention_policy_days": 365,
        }
        base.update(overrides)
        return base

    def test_returns_score_result(self, cfg):
        result = compute_score(self._app(), cfg)
        assert result.app_name == "Test App"
        assert 0 <= result.score <= 100

    def test_composite_equals_sum_of_weighted(self, cfg):
        result = compute_score(self._app(), cfg)
        expected = round(sum(fr.weighted_score for fr in result.factors), 2)
        assert result.score == expected

    def test_high_risk_app_is_critical(self, cfg):
        result = compute_score(self._app(
            record_count=9_000_000,
            data_amount_gb=8000,
            oldest_record_age_days=5000,
            retention_policy_days=365,
        ), cfg)
        assert result.tier == "Critical"

    def test_low_risk_app_is_exception(self, cfg):
        result = compute_score(self._app(
            business_function="internal",
            has_ssn=False,
            has_financial=False,
            has_cpni=False,
            record_count=100,
            data_amount_gb=0.5,
            oldest_record_age_days=30,
            retention_policy_days=3650,
        ), cfg)
        assert result.tier == "Exception"

    def test_factor_ids_match_config(self, cfg):
        result = compute_score(self._app(), cfg)
        config_ids = {f["id"] for f in cfg["factors"] if f.get("enabled", True)}
        result_ids = {fr.id for fr in result.factors}
        assert result_ids == config_ids

    def test_score_bounded_0_to_100(self, cfg):
        result = compute_score(self._app(
            record_count=10_000_000,
            data_amount_gb=10_000,
            oldest_record_age_days=365,
            retention_policy_days=365,
        ), cfg)
        assert 0 <= result.score <= 100

    def test_disabled_factor_excluded(self, cfg):
        factors = [
            {**f, "enabled": False} if f["id"] == "data_volume" else f
            for f in cfg["factors"]
        ]
        modified_cfg = {**cfg, "factors": factors}
        result = compute_score(self._app(), modified_cfg)
        ids = {fr.id for fr in result.factors}
        assert "data_volume" not in ids

    def test_bad_numeric_produces_warning(self, cfg):
        result = compute_score(self._app(record_count="lots"), cfg)
        assert any("record_count" in w for w in result.warnings)

    def test_missing_column_scores_0(self, cfg):
        row = self._app()
        del row["record_count"]
        result = compute_score(row, cfg)
        vol = next(fr for fr in result.factors if fr.id == "data_volume")
        assert vol.raw_score == 0.0

    def test_no_retention_policy_scores_max(self, cfg):
        result = compute_score(self._app(retention_policy_days=0), cfg)
        ret = next(fr for fr in result.factors if fr.id == "retention_risk")
        assert ret.raw_score == 100.0

    def test_unknown_factor_type_warns_and_scores_0(self, cfg):
        bad_factor = {
            "id": "mystery",
            "label": "Mystery",
            "enabled": True,
            "weight": 0.0,
            "type": "unknown_type",
            "column": "x",
        }
        modified = {**cfg, "factors": cfg["factors"] + [bad_factor]}
        result = compute_score(self._app(), modified)
        assert any("unknown_type" in w for w in result.warnings)
