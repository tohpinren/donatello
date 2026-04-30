# SQL Mutation Testing

SQL Mutation Testing is an open-source research tool for analyzing how well
Text-to-SQL benchmarks distinguish semantically correct SQL from plausible but
wrong SQL.

The core idea is simple: a benchmark database is part of the evaluation oracle.
If a meaningfully different SQL query still returns the same result on the
benchmark database, the benchmark is not testing that behavior strongly enough.

This repository builds a pipeline around that idea using:

- **SQLGlot** for SQL parsing and AST-level mutation
- **BIRD** as the primary benchmark target
- **Spider 2.0** as a planned secondary target, focused on the usable SQLite subset
- **SQLite** for query execution
- **VeriEQL** for equivalence filtering
- **SQLDriller** for counterexample generation

## Current Scope

What works today:

- BIRD benchmark normalization for `dev`, `train`, and `full`
- canonical mutation generation with all 19 mutation rules
- VeriEQL equivalence filtering with explicit proven equivalent, proven
  non-equivalent, timeout, unsupported, and error outcomes
- SQLite execution for retained proven non-equivalent mutants, with killed,
  survived, timeout, error, and mutation-score outputs
- SQLDriller counterexample generation for survived proven non-equivalent
  mutants, using SQLDriller `exec` mode only
- database augmentation from generated SQLDriller counterexamples, with
  conservative row merging into copied SQLite databases and augmented reruns
- Markdown and JSON reports for before/after mutation-score summaries
- resumable artifact-based runs driven by YAML config files
- `uv`-managed Python environment with Ruff, Ty, and pytest

What is planned next:

- Spider 2.0 SQLite-subset normalization
- model execution-accuracy comparison after the benchmark quality pipeline is
  reliable

## Install

### Requirements

- Python `3.11+`
- [`uv`](https://docs.astral.sh/uv/)
- benchmark data stored locally outside git

### Fresh Clone Setup

```bash
git clone <repo-url>
cd sql-mutation-testing

./scripts/bootstrap.sh
./scripts/check_env.sh
```

That will:

- create or sync the local `uv` environment
- verify Python and toolchain availability
- warn if optional external tools such as `VeriEQL` or `SQLDriller` are not on `PATH`

You can also run the setup steps directly:

```bash
uv sync
uv run sqlmt --help
uv run sqlmt check-env
uv run sqlmt check-env --config configs/smoke/bird-dev.yaml
```

### Docker Runner

The repository also supports a Dockerized runner for the full pipeline while
keeping the existing YAML config schema unchanged.

This setup uses:

- one resource-limited runner container for `sql-mutation-testing`
- one host-managed `verieql` sidecar for SQLDriller stage 6/7 integration
- bind mounts for benchmark data and `outputs/` so resumable artifacts persist
- a sidecar filesystem layout that mirrors SQLDriller's hard-coded
  `/verieql/main.py` exec path

Important current limitation:

- SQLDriller's counterexample path still talks to a Docker container named
  `verieql` through the host Docker socket
- stage 6/7 runs therefore require `/var/run/docker.sock` plus the
  repo-provided sidecar to be started first
- this Docker flow does **not** change `workers`, `resume`, `output_dir`, or
  any other YAML fields

The runner image is built from the current local checkouts, not upstream HEAD:

- this repo checkout
- the sibling `tools/VeriEQL` checkout
- the sibling `tools/SQLDriller` checkout
- the helper build scripts stage a temporary Docker build context from those
  working trees, so they do not require `docker buildx`

By default the helper scripts expect the following local layout:

```text
../sql-mutation-testing
../tools/VeriEQL
../tools/SQLDriller
```

You can override those source paths with `VERIEQL_SOURCE=...` and
`SQLDRILLER_SOURCE=...` when building.

Build the runner image:

```bash
scripts/build-runner-image.sh
```

Start the SQLDriller-compatible VeriEQL sidecar:

```bash
scripts/start-verieql-sidecar.sh
```

If a `verieql` or `verieql-mysql` container already exists, the helper now
fails fast instead of replacing it. Re-run with `--force` only when you want
to tear down and replace the existing sidecar pair explicitly.

Run the pipeline inside the runner container:

```bash
scripts/run-pipeline.sh \
  --config configs/experiments/bird-full-pipeline.yaml \
  --bird-root /path/to/bird \
  --outputs-root /path/to/sql-mutation-testing/outputs \
  --cpus 16 \
  --memory 200g
```

What the runner script does:

- mounts the host BIRD root read-only at `/opt/sqlmt/data/bird`
- mounts the host outputs directory read-write at `/opt/sqlmt/outputs`
- mounts `/var/run/docker.sock` so SQLDriller can exec into the `verieql`
  sidecar
- runs the container as the calling host UID/GID so mounted outputs stay
  user-owned on the server
- runs `uv run sqlmt run --config ...` inside the container with `--init`,
  `--cpus`, and `--memory`

Because artifacts are written to the mounted `outputs/` directory,
`resume: true` continues to be the restart mechanism after container restarts.

### VeriEQL Setup

VeriEQL is an external solver dependency and is not vendored into this
repository. Keep it in its own checkout and Python environment. The paths below
are examples; use the same environment variables in your experiment config.

```bash
export VERIEQL_ROOT=~/tools/VeriEQL
export VERIEQL_PYTHON=~/venvs/verieql/bin/python

git clone https://github.com/VeriEQL/VeriEQL.git "$VERIEQL_ROOT"
mkdir -p "$(dirname "$(dirname "$VERIEQL_PYTHON")")"
python3.11 -m venv "$(dirname "$VERIEQL_PYTHON")/.."
"$VERIEQL_PYTHON" -m pip install -r "$VERIEQL_ROOT/requirements.txt"
"$VERIEQL_PYTHON" -m pip install 'setuptools<81'
"$VERIEQL_PYTHON" -m pip install --force-reinstall 'z3-solver==4.15.0.0'
"$VERIEQL_PYTHON" - <<'PY'
import os
from pathlib import Path
import shutil
import z3

site_z3_dir = Path(z3.__file__).resolve().parent
source_dir = Path(os.environ["VERIEQL_ROOT"]).expanduser() / "z3py_libs"
for source in source_dir.glob("*.py"):
    shutil.copy2(source, site_z3_dir / source.name)
print(f"Patched Z3 Python files in {site_z3_dir}")
PY
```

The pipeline runs VeriEQL from that checkout as
`$VERIEQL_PYTHON -m parallel.cli_within_timeout` with the working directory set
to `$VERIEQL_ROOT`.

The extra Z3 patching step is required because VeriEQL relies on its own
`z3py_libs` Python wrappers, including APIs such as `And(..., ctx=...)` that do
not work with the stock `z3-solver` Python files alone.

### SQLDriller Setup

SQLDriller is also an external dependency and is not vendored into this
repository. Keep it in its own checkout and Python environment.

```bash
export SQLDRILLER_ROOT=~/tools/SQLDriller
export SQLDRILLER_PYTHON=~/venvs/sqldriller/bin/python

git clone https://github.com/SJTU-IPADS/SQLDriller.git "$SQLDRILLER_ROOT"
python3.11 -m venv "$(dirname "$SQLDRILLER_PYTHON")/.."
"$SQLDRILLER_PYTHON" -m pip install -r "$SQLDRILLER_ROOT/requirements.txt"
```

Stage 6 uses SQLDriller `exec` mode only through a thin repo-owned adapter. It
does not integrate SQLSolver/Java for this stage.

## Benchmark Data Setup

The repository does **not** ship benchmark data. Point configs to your local
benchmark root with `data_root` or environment variables such as `BIRD_ROOT`.

### Recommended Local Layout

```text
~/data/sqlmt/
  bird/
    dev/
      dev.json
      dev_tables.json
      dev_databases/
        <db_id>/
          <db_id>.sqlite
          database_description/
    train/
      train.json
      train_tables.json
      train_databases/
        <db_id>/
          <db_id>.sqlite
          database_description/
```

For the current BIRD implementation:

- `dev`: 1534 questions across 11 SQLite databases
- `train`: 9428 questions across 69 SQLite databases
- `full`: `dev + train`, for 10962 questions across 80 SQLite databases

### Set `BIRD_ROOT`

```bash
export BIRD_ROOT=~/data/sqlmt/bird
```

Or set it inline per command:

```bash
BIRD_ROOT=~/data/sqlmt/bird uv run sqlmt run --config configs/smoke/bird-dev.yaml
```

## Quick Start

### 1. Validate a Config

```bash
uv run sqlmt config validate --config configs/smoke/bird-dev.yaml
```

### 2. Dry Run

Use `--dry-run` to inspect resolved outputs without creating artifacts.

```bash
uv run sqlmt run --config configs/smoke/bird-dev.yaml --dry-run
```

### 3. Normalize BIRD

```bash
BIRD_ROOT=~/data/sqlmt/bird uv run sqlmt run --config configs/smoke/bird-dev.yaml
```

### 4. Normalize and Generate Mutants

```bash
BIRD_ROOT=~/data/sqlmt/bird uv run sqlmt run --config configs/smoke/bird-dev-mutate.yaml
```

### 5. Full BIRD Normalization

```bash
BIRD_ROOT=~/data/sqlmt/bird uv run sqlmt run --config configs/experiments/bird-full.yaml
```

`configs/experiments/bird-full.yaml` currently enables normalization only. For a
full mutation-generation run, copy that config and set `stages.mutate: true`.

## Configuration

Runs are driven by YAML config files under [`configs/`](configs/).

Main fields:

- `benchmark`: currently `bird`
- `bird_mode`: `dev`, `train`, or `full`
- `data_root`: benchmark root path
- `output_dir`: artifact directory for the run
- `mutation_profile`: currently `canonical`
- `workers`: concurrency used by VeriEQL, SQLite execution, and SQLDriller
  counterexample generation
- `resume` / `overwrite`: artifact reuse behavior
- `stages`: enable `normalize`, `mutate`, `equivalence`, `execute`,
  `counterexamples`, `augment`, and `report`

Example local config:

```yaml
experiment_name: local-bird-dev
benchmark: bird
bird_mode: dev
data_root: ~/data/sqlmt/bird
output_dir: outputs/local-bird-dev
mutation_profile: canonical
workers: 4
resume: true
overwrite: false

stages:
  normalize: true
  mutate: true
  equivalence: true
  execute: true
  counterexamples: false
  augment: false
  report: false

timeouts:
  verieql_seconds: 120
  sqlite_seconds: 120
  sqldriller_seconds: 300

tools:
  verieql_path: ${VERIEQL_ROOT}
  verieql_python: ${VERIEQL_PYTHON}
  sqldriller_path: ${SQLDRILLER_ROOT}
  sqldriller_python: ${SQLDRILLER_PYTHON}
```

Use [`configs/templates/local.yaml.example`](configs/templates/local.yaml.example)
as the starting point for machine-specific configs.

### Running Augmentation and Reporting

Augmentation is intended to run after counterexamples exist. A typical follow-up
config reuses an existing experiment output directory and enables only the final
stages:

```yaml
resume: true
overwrite: false

stages:
  normalize: false
  mutate: false
  equivalence: false
  execute: false
  counterexamples: false
  augment: true
  report: true
```

Then run:

```bash
uv run sqlmt run --config configs/local/<name>.yaml
```

`augment` requires `mutants.jsonl`, `equivalence.jsonl`, `execution.jsonl`, and
`counterexamples.jsonl` to already exist unless those upstream stages are
enabled in the same run. `report` can run on partial outputs and marks missing
sections as unavailable.

## Artifacts

Each run writes to its own directory under `outputs/`.

Current artifacts:

```text
outputs/
  <experiment-name>/
    config.yaml
    run.log
    normalized_examples.jsonl
    mutants.jsonl
    equivalence.jsonl
    execution.jsonl
    counterexamples.jsonl
    counterexamples/
    augmented_databases/
      augmentation.jsonl
    execution_augmented.jsonl
    report.md
    report.json
```

Current stage behavior:

- normalization writes `normalized_examples.jsonl`
- mutation generation reads `normalized_examples.jsonl` and writes `mutants.jsonl`
- equivalence filtering reads `normalized_examples.jsonl` and `mutants.jsonl`,
  runs VeriEQL, and writes `equivalence.jsonl`
- SQLite execution reads `mutants.jsonl` and `equivalence.jsonl`, runs only
  `proven_non_equivalent` mutants, applies `timeouts.sqlite_seconds` per query,
  and writes `execution.jsonl`
- counterexample generation reads `mutants.jsonl`, `equivalence.jsonl`, and
  `execution.jsonl`, runs only `survived` plus `proven_non_equivalent` mutants,
  uses SQLDriller `exec` mode only, writes `counterexamples.jsonl`, and stores
  minimized SQLite files under `counterexamples/`
- augmentation reads `mutants.jsonl`, `equivalence.jsonl`, `execution.jsonl`,
  and `counterexamples.jsonl`, copies original SQLite databases into
  `augmented_databases/`, conservatively merges generated counterexample rows,
  reruns covered mutants against the augmented copies, writes
  `augmented_databases/augmentation.jsonl` and `execution_augmented.jsonl`, and
  recomputes mutation scores
- reporting reads available artifacts and writes `report.md` plus `report.json`
- `resume: true` reuses existing artifacts
- `overwrite: true` replaces them

The execution mutation score is:

```text
killed / (killed + survived)
```

Execution timeouts and errors are reported separately and excluded from the score.
Augmented execution uses the same formula after counterexample rows have been
merged into copied databases.

`workers` currently applies to:

- VeriEQL batch checking
- SQLite execution for retained mutants
- SQLDriller counterexample generation for retained survivors

## Mutation Profile

The current implementation ships one canonical mutation profile with 19 rules:

- Set A: `a_null_pred`, `a_topk_ties`, `a_agg_edge`, `a_pred_loc`, `a_subq_corr_drop`
- Set B: `b_join_miss`, `b_from_unused_tv`, `b_subq_scope_typo`, `b_like_nowildcard`, `b_wildcard_wrong_op`, `b_domain_mismatch_comp`
- Set C: `c_pred_ror`, `c_pred_lcr`, `c_multi_dist`, `c_multi_setop`, `c_group_agg`, `c_group_key`, `c_bin_join`, `c_sub_qual`

Each rule either:

- emits one or more SQLGlot-validated mutants, or
- records a clean skip reason such as `rule_not_applicable`, `duplicate_sql`, or `same_as_gold`

The generator currently keeps at most `3` generated mutants per rule for each
gold SQL. That default is centralized in
`src/sqlmt/mutation/generator.py` as
`DEFAULT_MAX_GENERATED_MUTANTS_PER_RULE`, and `generate_mutations(...)` also
accepts `max_generated_per_rule=...` if you want to override it in code.

## CLI

```bash
uv run sqlmt --help
uv run sqlmt check-env
uv run sqlmt config validate --config <config.yaml>
uv run sqlmt run --config <config.yaml>
uv run sqlmt run --config <config.yaml> --dry-run
```

## Development

```bash
uv sync
uv run ruff check .
uv run ruff format .
uv run ty check
uv run pytest
```
