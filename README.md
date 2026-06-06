# pop-score — Privacy Onboarding Priority Scoring Engine

pop-score helps privacy and compliance teams answer: **which enterprise applications should we onboard to DSR/privacy workflows first?**

It scores each application across five configurable factors, produces an explainable 0–100 Signal Score, and outputs a ranked CSV and JSON audit file. Scoring is triggered monthly by a **ServiceNow Scheduled Job** that detects new deployments and database changes, initiates a live DB metadata scan, and writes results back to ServiceNow.

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

## How it works

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
        "data_sensitivity":  { "raw": 100.0, "weighted": 25.0 },
        ...
      }
    }
  ]
}
```

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
