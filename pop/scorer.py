"""
pop.scorer
~~~~~~~~~~
Config-driven scoring engine for the Privacy Onboarding Priority (POP) model.

All scoring logic is dispatched from ``config.yaml`` — no factor definitions
live in this file.  To add a factor, change a weight, or rename a CSV column,
edit config.yaml only; no code changes or redeployment required.

Supported factor types
─────────────────────
lookup        Map a string column value to a score via a lookup table.
log_scale     Log₁₀-scale a numeric column against a reference ceiling.
max_flag      Score = MAX of the scores for all truthy boolean flag columns.
ratio         Score = (numerator_column / denominator_column) × 100, capped 100.
linear_scale  Linear-scale a numeric column between a min and max.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class FactorResult:
    """Score and metadata for one factor."""
    id: str
    label: str
    raw_score: float       # normalised 0–100
    weighted_score: float  # raw_score × weight
    weight: float


@dataclass
class ScoreResult:
    """Full scoring output for one application."""
    app_name: str
    score: float                            # composite 0–100
    tier: str                               # Critical / High / Medium / Exception
    factors: list[FactorResult]
    warnings: list[str] = field(default_factory=list)


# ── Config validation ─────────────────────────────────────────────────────────

def validate_config(config: dict[str, Any]) -> list[str]:
    """
    Return a list of validation warnings for the config.
    Does not raise — callers decide whether warnings are fatal.
    """
    issues: list[str] = []

    factors = [f for f in config.get("factors", []) if f.get("enabled", True)]
    if not factors:
        issues.append("No enabled factors found in config.")
        return issues

    weight_sum = sum(f.get("weight", 0) for f in factors)
    if abs(weight_sum - 1.0) > 0.01:
        issues.append(
            f"Enabled factor weights sum to {weight_sum:.4f} (expected 1.0). "
            "Scores will be computed with these weights; consider rebalancing."
        )

    valid_types = {"lookup", "log_scale", "max_flag", "ratio", "linear_scale"}
    for f in factors:
        fid = f.get("id", "<unnamed>")
        ftype = f.get("type", "")
        if ftype not in valid_types:
            issues.append(f"Factor '{fid}': unknown type '{ftype}'. Valid: {sorted(valid_types)}.")
        if f.get("weight", 0) <= 0:
            issues.append(f"Factor '{fid}': weight must be > 0.")

    tiers = config.get("tiers", {})
    for key in ("critical", "high", "medium"):
        if key not in tiers:
            issues.append(f"Tier '{key}' is missing from config.")

    return issues


# ── Factor type implementations ───────────────────────────────────────────────

def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


def _get_float(row: dict, column: str, warnings: list[str]) -> float:
    val = row.get(column, 0)
    try:
        return float(val) if str(val).strip() != "" else 0.0
    except (ValueError, TypeError):
        warnings.append(f"Non-numeric value for column '{column}': {val!r} — defaulted to 0")
        return 0.0


def _score_lookup(factor: dict, row: dict, warnings: list[str]) -> float:
    """Map a string column value to a score via a lookup table."""
    column = factor["column"]
    values: dict = factor["lookup"]["values"]
    default = float(factor["lookup"].get("default", min(values.values())))
    key = str(row.get(column, "")).strip().lower()
    if key in values:
        return float(values[key])
    warnings.append(
        f"Factor '{factor['id']}': unrecognised value '{key}' in column '{column}' "
        f"— using default score {default}."
    )
    return default


def _score_log_scale(factor: dict, row: dict, warnings: list[str]) -> float:
    """Log₁₀-scale a numeric column against a configured ceiling."""
    column = factor["column"]
    ref_max = float(factor["reference_max"])
    value = _get_float(row, column, warnings)
    if value <= 0:
        return 0.0
    raw = math.log10(value) / math.log10(ref_max)
    return min(raw * 100, 100.0)


def _score_max_flag(factor: dict, row: dict, _warnings: list[str]) -> float:
    """Score = MAX of the scores for all truthy boolean flag columns."""
    flags: dict = factor["flags"]
    default = float(factor.get("default", 10))
    scores = [
        float(score)
        for col, score in flags.items()
        if _is_truthy(row.get(col, False))
    ]
    return max(scores) if scores else default


def _score_ratio(factor: dict, row: dict, warnings: list[str]) -> float:
    """Score = (numerator / denominator) × 100, capped at 100."""
    num_col = factor["numerator_column"]
    den_col = factor["denominator_column"]
    no_den_score = float(factor.get("no_denominator_score", 100))

    numerator = _get_float(row, num_col, warnings)
    denominator = _get_float(row, den_col, warnings)

    if denominator <= 0:
        return no_den_score
    return min((numerator / denominator) * 100, 100.0)


def _score_linear_scale(factor: dict, row: dict, warnings: list[str]) -> float:
    """Linear-scale a numeric column between a configured min and max."""
    column = factor["column"]
    scale_min = float(factor.get("min", 0))
    scale_max = float(factor["max"])
    value = _get_float(row, column, warnings)
    if scale_max <= scale_min:
        warnings.append(f"Factor '{factor['id']}': linear_scale max <= min — scored 0.")
        return 0.0
    clamped = max(scale_min, min(value, scale_max))
    return (clamped - scale_min) / (scale_max - scale_min) * 100


# ── Factor dispatcher ─────────────────────────────────────────────────────────

_DISPATCHERS = {
    "lookup":       _score_lookup,
    "log_scale":    _score_log_scale,
    "max_flag":     _score_max_flag,
    "ratio":        _score_ratio,
    "linear_scale": _score_linear_scale,
}


def _score_factor(factor: dict, row: dict, warnings: list[str]) -> float:
    ftype = factor.get("type", "")
    dispatcher = _DISPATCHERS.get(ftype)
    if dispatcher is None:
        warnings.append(f"Factor '{factor.get('id', '?')}': unknown type '{ftype}' — scored 0.")
        return 0.0
    return round(dispatcher(factor, row, warnings), 2)


# ── Tier assignment ───────────────────────────────────────────────────────────

def assign_tier(score: float, config: dict[str, Any]) -> str:
    """Map a composite score to a named tier using config thresholds."""
    t = config["tiers"]
    if score >= t["critical"]:
        return "Critical"
    if score >= t["high"]:
        return "High"
    if score >= t["medium"]:
        return "Medium"
    return "Exception"


# ── Composite scorer ──────────────────────────────────────────────────────────

def compute_score(row: dict[str, Any], config: dict[str, Any]) -> ScoreResult:
    """
    Compute a full POP ScoreResult for one application row.

    Factors are driven entirely by config.yaml — no column names or scoring
    logic are hardcoded here.  Add/remove/retune factors in config only.
    """
    warnings: list[str] = []

    enabled_factors = [f for f in config.get("factors", []) if f.get("enabled", True)]

    factor_results: list[FactorResult] = []
    for factor in enabled_factors:
        raw = _score_factor(factor, row, warnings)
        weight = float(factor.get("weight", 0))
        factor_results.append(FactorResult(
            id=factor["id"],
            label=factor.get("label", factor["id"]),
            raw_score=raw,
            weighted_score=round(raw * weight, 2),
            weight=weight,
        ))

    composite = round(sum(fr.weighted_score for fr in factor_results), 2)
    tier = assign_tier(composite, config)

    return ScoreResult(
        app_name=str(row.get("app_name", "unknown")),
        score=composite,
        tier=tier,
        factors=factor_results,
        warnings=warnings,
    )
