# POP-Score — Privacy Onboarding Priority Scoring Engine

**POP-Score answers one deceptively hard question for privacy and compliance teams: _which enterprise application should we onboard to DSR and privacy workflows first?_**

---

## The problem

Large enterprises run thousands of applications, with personal data scattered across fragmented databases, data lakes, and warehouses. New data platforms, AI agents, and integrations spin up almost daily - each one a fresh source of compliance risk. No team can onboard everything at once, so the recurring question is always the same: *what do we tackle first?*

Most organizations answer this subjectively. Prioritization comes down to tribal knowledge, the loudest stakeholder, or whoever's most recently been audited. The result is inconsistent, hard to explain, and impossible to defend when a regulator or internal auditor asks *"why this app and not that one?"*

POP-Score replaces that guesswork with a systematic, data-driven methodology. Every application is scored on the factors that genuinely drive compliance risk — what kind of data it holds, how much, how sensitive it is, and how long it's been retained. It's a **risk-based score: the higher the risk, the higher the priority.**

The model is grounded in established privacy regulation - GDPR, CCPA, and the EU AI Act and reflects Privacy by Design principles: data minimization, retention limits, Records of Processing Activities (RoPA), data inventory, sensitive-data identification, and financial/fraud-detection checks. The output is a transparent, auditable ranking that compliance teams can stand behind.

---

## The systems-thinking shift

The deeper idea behind POP-Score is that **prioritization should be a property of the system, not a recurring manual exercise.**

Traditional privacy onboarding treats every audit and every prioritization decision as an isolated event a fire drill where teams scramble to reconstruct a defensible narrative after the fact. POP-Score inverts that. It builds prioritization into the operating fabric: a scheduled job continuously detects new and changed applications, scans their data, scores them against consistent criteria, and writes the result back into the system of record.

Three principles drive the design:

1. **The score is explainable, not a black box.** Every composite score decomposes into its factors and weighted contributions, so any stakeholder can trace *exactly* why an app landed where it did.
2. **Policy is configuration, not code.** Weights, thresholds, sensitivity flags, and tier boundaries all live in `config.yaml`. When legal guidance changes, you edit a value — you don't redeploy.
3. **Detection is continuous, not periodic.** The system watches for change and rescans only what moved, so prioritization stays current without re-scanning the entire estate every cycle.

This is the difference between *running an audit* and *operating an audit-ready system.*

---

## How the pieces fit together

POP-Score scores each application across five configurable factors, produces an explainable 0–100 composite score, and outputs a ranked CSV plus a JSON audit file. The full cycle is triggered monthly by a **ServiceNow Scheduled Job** that detects new deployments and database changes, runs a live DB metadata scan, scores the results, and writes them back to ServiceNow.

```
ServiceNow Scheduled Job (monthly)
        │
        ▼
python -m pop.cli monitor
        │
        ├─ 1. Pull active application CIs from cmdb_ci_appl
        │
        ├─ 2. Pull change_request records (last 35 days)
        │      → only changed/deployed apps are rescanned
        │      → unchanged apps are skipped (faster cycle, lower DB load)
        │
        ├─ 3. DB metadata scan per changed app
        │      → record_count, storage_gb, oldest_record_age_days
        │      → supports: mock | sqlite | postgresql | mysql
        │
        ├─ 4. POP scoring engine
        │      → 5 weighted factors → composite 0–100 score → tier
        │
        ├─ 5. Write scores back to ServiceNow (u_pop_score_result)
        │      → optional, configured via write_back.enabled
        │
        └─ 6. Emit pop-run-YYYYMMDD-HHMMSS.csv + .json for audit
```

---

## Quick start

```bash
pip install -r requirements.txt

# Score from a static CSV (ad-hoc / offline)
python -m pop.cli score apps.csv --output results.csv

# Run a full monthly monitor cycle (mock mode — no real ServiceNow needed)
python -m pop.cli monitor --force-all
```

---

## The scoring model

### Composite score

```
score = Σ (factor_score_i × weight_i)     for all enabled factors
```

Each factor is normalised to **0–100** before weighting. The composite is also 0–100.

### Tiers

| Score | Tier | Recommended action |
|-------|------|--------------------|
| ≥ 80 | **Critical** | Immediate onboarding |
| 60–79 | **High** | Next sprint |
| 40–59 | **Medium** | Backlog — scheduled |
| < 40 | **Exception** | Deprioritised / document rationale |

The **Exception** tier is a deliberate governance control, not just a low score. Anything that lands here should carry a documented, signed-off rationale for *why* it was deprioritised — so the decision is auditable rather than silent.

---

## Worked example

To see the model end-to-end, take **Salesforce CRM**, a consumer-facing app holding SSNs, ~8M records, ~2TB of data, with its oldest record sitting right at the retention boundary.

| Factor | Raw input | Raw score | Weight | Weighted |
|--------|-----------|-----------|--------|----------|
| `business_function` | `consumer` | 100.0 | 0.25 | 25.0 |
| `data_sensitivity` | `has_ssn = true` | 100.0 | 0.25 | 25.0 |
| `data_volume` | 8,000,000 records | 90.3 | 0.20 | 18.1 |
| `data_amount_gb` | 2,048 GB | 76.3 | 0.15 | 11.4 |
| `retention_risk` | at policy boundary | 100.0 | 0.15 | 15.0 |
| **Composite** | | | | **94.5 → Critical** |

A reader can trace every point of that 94.5 back to a real, sourced input — which is exactly what an auditor wants to see.

---

## Factors and rationale

### 1. `business_function` — weight 0.25

| Value | Score | Rationale |
|-------|-------|-----------|
| `consumer` | 100 | Direct data subject interaction; full CCPA/GDPR DSR obligation |
| `marketing` | 100 | Consumer data used for targeting; same regulatory exposure |
| `internal` | 40 | Employee data; DSR obligations exist but are narrower |
| `backend` | 20 | Typically derivative/processed data; lower direct exposure |

**Why 0.25?** Business function determines the *type* of DSR obligation. A consumer-facing app with moderate volume creates more legal risk than a large internal system.

### 2. `data_sensitivity` — weight 0.25

Scored as the **maximum** sensitivity of any data type flag present:

| Flag | Score | Regulation |
|------|-------|------------|
| `has_ssn` | 100 | State data breach laws, CCPA |
| `has_financial` | 85 | PCI-DSS, GLBA |
| `has_cpni` | 70 | CPNI/TCPA |
| *(none)* | 10 | General PII only |

Uses MAX rather than sum — a single SSN field creates full regulatory exposure regardless of other lower-sensitivity fields.

**Why 0.25?** Data type is equally as important as business function in determining regulatory surface.

### 3. `data_volume` — weight 0.20

Log₁₀-scaled record count against a configurable ceiling (default 10 M = 100).

```
score = min( log10(record_count) / log10(10_000_000) × 100, 100 )
```

**Why log-scaled?** The jump from 10 K → 100 K records matters more than 8 M → 9 M. Log scale reflects diminishing marginal risk.

### 4. `data_amount_gb` — weight 0.15

Log₁₀-scaled storage footprint (default 10 TB = 100). Independent of record count — catches wide-column stores, binary blobs, and log data that row counts miss.

### 5. `retention_risk` — weight 0.15

How far the oldest record has drifted relative to the stated retention policy:

```
score = min( oldest_record_age_days / retention_policy_days × 100, 100 )
```

At or past policy boundary → 100. No defined policy → 100 (unknown = maximum risk).

**Why 0.15?** Retention overruns create direct GDPR Art. 5(1)(e) and CCPA risk.

### A note on the weights

The two highest-leverage factors — business function and data sensitivity — carry the most weight (0.25 each) because together they define the *regulatory surface*: who the data subjects are and how exposed their data is. Volume and the two newer factors (storage footprint and retention drift) are amplifiers — they make an already-risky app riskier, but they don't create obligation on their own. Hence 0.20 and 0.15. Because every weight is configurable, these are starting positions a privacy office can tune to its own risk appetite, not fixed law.

---

## Configuration — everything lives in `config.yaml`

No code changes or redeployments needed for any of the following:

| What you want to change | How |
|------------------------|-----|
| Adjust a weight | Edit `weight:` on any factor |
| Add a new business type | Add a key under `lookup.values` |
| Add a sensitivity flag (e.g. `has_phi: 95`) | Add a key under `flags` |
| Rename a CSV/CMDB column | Change `column:` on the factor |
| Add a brand-new factor | Add an entry to `factors:` list |
| Disable a factor temporarily | Set `enabled: false` |
| Change scaling ceiling | Edit `reference_max` |
| Change tier thresholds | Edit `tiers:` section |
| Connect to real ServiceNow | Set `mock_mode: false` + credentials |
| Write scores back to ServiceNow | Set `write_back.enabled: true` |
| Change change-detection window | Edit `monitor.lookback_days` |

### Supported factor types

| Type | Description |
|------|-------------|
| `lookup` | Map a string value to a score via a table |
| `log_scale` | Log₁₀-scale a numeric column against a ceiling |
| `max_flag` | Max score across present boolean flag columns |
| `ratio` | `(numerator / denominator) × 100`, capped at 100 |
| `linear_scale` | Linear scale between a min and max |

---

## ServiceNow integration

### Connecting to your instance

Set credentials as environment variables — never in config:

```bash
export SN_USERNAME=your_sn_user
export SN_PASSWORD=your_sn_password
```

Update `config.yaml`:

```yaml
servicenow:
  instance:   "https://yourco.service-now.com"
  username:   "${SN_USERNAME}"
  password:   "${SN_PASSWORD}"
  mock_mode:  false

  write_back:
    enabled: true
    table:   "u_pop_score_result"
```

### Field mappings

Map POP model fields to your ServiceNow CI field names — no code changes:

```yaml
field_mappings:
  app_name:              "name"
  business_function:     "u_business_function"
  has_ssn:               "u_has_ssn"
  has_financial:         "u_has_financial"
  has_cpni:              "u_has_cpni"
  retention_policy_days: "u_retention_policy_days"
  db_connection_json:    "u_db_connection_json"
```

### Setting up the monthly trigger in ServiceNow

1. Create a **Scheduled Script Execution** (System Definition → Scheduled Jobs)
2. Set frequency: **Monthly**
3. Script:

```javascript
// ServiceNow Scheduled Job — POP Monitor trigger
var cmd = new GlideRecord('sys_trigger');
// Call your hosted pop-score endpoint or use MID Server to run:
// python -m pop.cli monitor --config /path/to/config.yaml
```

Or trigger via a **MID Server** shell command:

```bash
python -m pop.cli monitor --config /etc/pop-score/config.yaml --output-dir /var/pop-score/runs/
```

---

## Database scanner

The scanner connects to each app's database to retrieve live metadata. Connection info is stored in the CI's `u_db_connection_json` field in ServiceNow.

### Supported database types

| Type | Requirements |
|------|-------------|
| `mock` | None — synthetic data for demo/dev |
| `sqlite` | Standard library |
| `postgresql` | `pip install psycopg2-binary` |
| `mysql` | `pip install mysql-connector-python` |

### Example connection info (stored in ServiceNow CI)

```json
{
  "type": "postgresql",
  "host": "db.internal.yourco.com",
  "port": 5432,
  "database": "salesforce_mirror",
  "username_env": "SFCRM_DB_USER",
  "password_env": "SFCRM_DB_PASS"
}
```

Credentials are resolved from environment variables at scan time — never stored in ServiceNow or config.

### Configuring date columns for oldest-record detection

```yaml
db_scanner:
  date_columns:
    - table: "contacts"
      column: "created_at"
    - table: "orders"
      column: "order_date"
```

---

## CLI reference

```bash
# Score from a static CSV
python -m pop.cli score apps.csv
python -m pop.cli score apps.csv --config config.yaml --output results.csv

# Run monthly monitor cycle
python -m pop.cli monitor
python -m pop.cli monitor --force-all          # rescan all apps regardless of changes
python -m pop.cli monitor --dry-run            # mock mode, no ServiceNow writes
python -m pop.cli monitor --output-dir ./runs  # write CSV + JSON to a directory
python -m pop.cli monitor --config /etc/pop-score/config.yaml
```

---

## Output files

Every monitor run produces two files named `pop-run-YYYYMMDD-HHMMSS.*`:

### `.csv` — ranked results

| Column | Description |
|--------|-------------|
| `run_id` | Unique run identifier |
| `app_name` | Application name |
| `score` | Composite 0–100 POP score |
| `tier` | Critical / High / Medium / Exception |
| `factor_<id>` | Raw factor score 0–100 (one per factor) |
| `weighted_<id>` | Weighted contribution (raw × weight) |
| `scan_warnings` | DB scanner warnings |
| `score_warnings` | Scoring engine warnings |

### `.json` — run summary (for dashboards / audit)

```json
{
  "run_id": "pop-run-20260606-034907",
  "started_at": "2026-06-06T03:49:07Z",
  "apps_found": 10,
  "apps_scanned": 6,
  "apps_skipped": 4,
  "tier_counts": { "Critical": 2, "High": 7, "Medium": 1 },
  "results": [
    {
      "app_name": "Salesforce CRM",
      "score": 95.26,
      "tier": "Critical",
      "factors": {
        "business_function": { "raw": 100.0, "weighted": 25.0 },
        "data_sensitivity":  { "raw": 100.0, "weighted": 25.0 }
      }
    }
  ]
}
```

---

## Limitations and assumptions

POP-Score is a prioritization aid, not a compliance determination. A few things worth being explicit about:

- **Scoring quality depends on metadata accuracy.** The model is only as good as the `business_function`, sensitivity flags, and retention policies recorded in ServiceNow and the source databases. Garbage in, garbage out applies.
- **Log-scaling is a deliberate choice.** It prevents a single massive application from dominating the ranking purely on size. Teams that want size to dominate can switch to `linear_scale` in config.
- **"Unknown = maximum risk"** for retention. An app with no defined retention policy scores 100 on that factor by design — absence of a policy is itself a finding.
- **The weights are a starting position, not law.** They encode one reasonable risk philosophy; every privacy office should tune them to its own regulatory footprint and risk appetite.

---

## Roadmap

- REST API endpoint for on-demand scoring outside the monthly cycle
- Web dashboard for tier distribution, onboarding velocity, and unassessed-app aging
- Native Collibra connector for data-catalog-driven sensitivity flags
- Pluggable scoring for emerging AI/agent data platforms (EU AI Act alignment)
- Historical score tracking to surface apps whose risk is *trending* up

---

## Running tests

```bash
pytest tests/ -v
```

92 tests covering all factor types, tier boundaries, ServiceNow mock client, DB scanner, full monitor cycle, and output writers.

---

## Project structure

```
pop-score/
  pop/
    __init__.py
    scorer.py               # pure, config-driven scoring engine
    monitor.py              # monthly orchestrator
    cli.py                  # CLI — score | monitor commands
    __main__.py
    connectors/
      __init__.py
      servicenow.py         # ServiceNow CMDB + change API client
      db_scanner.py         # DB metadata scanner (mock/sqlite/postgresql/mysql)
  tests/
    test_scorer.py          # 63 scoring engine tests
    test_monitor.py         # 29 connector + monitor tests
  config.yaml               # all weights, factors, thresholds, SN config
  apps.csv                  # sample input for score command
  requirements.txt
  README.md
```

---

## Requirements

```
pyyaml>=6.0
pytest>=8.0

# Optional — only needed for real database scanning:
# psycopg2-binary   (PostgreSQL)
# mysql-connector-python  (MySQL)
```
