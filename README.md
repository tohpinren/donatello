# Donatello

Donatello is an open-source research tool for analyzing how well
Text-to-SQL benchmarks distinguish semantically correct SQL from plausible but
wrong SQL.

The core idea is simple: a benchmark database is part of the evaluation oracle.
If a meaningfully different SQL query still returns the same result on the
benchmark database, the benchmark is not testing that behavior strongly enough.

This repository builds a pipeline around that idea using:

- **SQLGlot** for SQL parsing and AST-level mutation
- **BIRD** as the primary benchmark target
- **Spider 2.0** for the usable SQLite subset
- **SQLite** for query execution
- **VeriEQL** for equivalence filtering
- **SQLDriller** for counterexample generation

## Framework Capabilities

- BIRD benchmark normalization for `dev`, `train`, and `full`
- Spider 2.0 normalization for the paper's SQLite-backed 24-question subset
- canonical mutation generation with all 19 mutation rules
- VeriEQL equivalence filtering with explicit proven equivalent, proven
  non-equivalent, timeout, unsupported, and error outcomes
- SQLite execution for retained proven non-equivalent mutants, with killed,
  survived, timeout, error, and mutation-score outputs
- SQLDriller counterexample generation for survived proven non-equivalent
  mutants, using SQLDriller `exec` mode
- database augmentation from generated SQLDriller counterexamples, with
  conservative row merging into copied SQLite databases and augmented reruns
- Markdown and JSON reports for before/after mutation-score summaries
- resumable artifact-based runs driven by YAML config files
- `uv`-managed Python environment with Ruff, Ty, and pytest
- standalone counterexample-transfer evaluation utilities for comparison with
  prior counterexample-based evaluation workflows

Benchmark datasets, model prediction artifacts, SpotIT artifacts, and generated
outputs are intentionally not committed to this repository. Keep those files in
local data/output directories and pass their locations through configs or script
arguments.

## Install

### Requirements

- Python `3.11+`
- [`uv`](https://docs.astral.sh/uv/)
- benchmark data stored locally outside git

### Fresh Clone Setup

```bash
git clone <repo-url>
cd donatello

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
uv run donatello --help
uv run donatello check-env
uv run donatello check-env --config configs/smoke/bird-dev.yaml
```

### Docker Runner

The repository also supports a Dockerized runner for the full pipeline while
keeping the existing YAML config schema unchanged.

This setup uses:

- one resource-limited runner container for `donatello`
- one host-managed `verieql` sidecar for SQLDriller stage 6/7 integration
- bind mounts for benchmark data and `outputs/` so resumable artifacts persist
- a sidecar filesystem layout that mirrors SQLDriller's hard-coded
  `/verieql/main.py` exec path

Docker architecture note:

- SQLDriller's counterexample path still talks to a Docker container named
  `verieql` through the host Docker socket
- stage 6/7 runs therefore require `/var/run/docker.sock` plus the
  repo-provided sidecar to be started first
- this Docker flow does **not** change `workers`, `resume`, `output_dir`, or
  any other YAML fields

The runner image is built from local checkouts, not upstream HEAD:

- this repo checkout
- the sibling `tools/VeriEQL` checkout
- the sibling `tools/SQLDriller` checkout
- the helper build scripts stage a temporary Docker build context from those
  working trees, so they do not require `docker buildx`

By default the helper scripts expect the following local layout:

```text
../donatello
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

The sidecar helper now creates a fixed Docker subnet by default:

```text
donatello-verieql -> 10.50.0.0/24
```

That avoids Docker auto-assigning a `192.168.x.x` range that could overlap
with lab/VPN client routes. You can override the defaults with
`VERIEQL_NETWORK=...` and `VERIEQL_SUBNET=...` if needed.

If a `verieql` or `verieql-mysql` container already exists, the helper now
fails fast instead of replacing it. Re-run with `--force` only when you want
to tear down and replace the existing sidecar pair explicitly.

Run the pipeline inside the runner container:

```bash
scripts/run-pipeline.sh \
  --config configs/experiments/bird-full-pipeline.yaml \
  --bird-root /path/to/bird \
  --outputs-root /path/to/donatello/outputs \
  --cpus 16 \
  --memory 200g
```

If a pipeline container with the chosen name already exists, the helper now
fails fast instead of replacing it. Re-run with `--force` only when you want
to stop and replace the existing pipeline container explicitly.

What the runner script does:

- mounts the host BIRD root read-only at `/opt/donatello/data/bird`
- mounts the host outputs directory read-write at `/opt/donatello/outputs`
- mounts `/var/run/docker.sock` so SQLDriller can exec into the `verieql`
  sidecar
- runs the container as the calling host UID/GID so mounted outputs stay
  user-owned on the server
- runs `uv run donatello run --config ...` inside the container with `--init`,
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

Stage 6 uses SQLDriller `exec` mode through a thin repo-owned adapter. SQLSolver
and Java are not required for this stage.

## Benchmark Data Setup

The repository does **not** ship benchmark data. Point configs to your local
benchmark root with `data_root` or environment variables such as `BIRD_ROOT`.

### BIRD Layout

```text
~/data/donatello/
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

Supported BIRD splits:

- `dev`: 1534 questions across 11 SQLite databases
- `train`: 9428 questions across 69 SQLite databases
- `full`: `dev + train`, for 10962 questions across 80 SQLite databases

### Set `BIRD_ROOT`

```bash
export BIRD_ROOT=~/data/donatello/bird
```

Or set it inline per command:

```bash
BIRD_ROOT=~/data/donatello/bird uv run donatello run --config configs/smoke/bird-dev.yaml
```

### Spider 2.0 SQLite Subset Layout

The Spider 2.0 adapter targets the 24 SQLite-backed local questions used in the
paper experiments. The expected data root is:

```text
~/data/donatello/spider2/
  spider2-lite.jsonl
  gold_sql/
    local003.sql
    ...
  local_sqlite/
    local-map.jsonl
    <db_name>.sqlite
```

`local-map.jsonl` is expected to contain a JSON object mapping
`instance_id -> db_name`. The adapter keeps only the 24 expected local IDs and
fails fast if the intersection of metadata, gold SQL, and local SQLite mappings
does not match that set. Set:

```bash
export SPIDER2_ROOT=~/data/donatello/spider2
export DONATELLO_OUTPUT_ROOT=outputs
```

Then run:

```bash
uv run donatello run --config configs/experiments/spider2-sqlite24-full.yaml
```

## Quick Start

### 1. Validate a Config

```bash
uv run donatello config validate --config configs/smoke/bird-dev.yaml
```

### 2. Dry Run

Use `--dry-run` to inspect resolved outputs without creating artifacts.

```bash
uv run donatello run --config configs/smoke/bird-dev.yaml --dry-run
```

### 3. Normalize BIRD

```bash
BIRD_ROOT=~/data/donatello/bird uv run donatello run --config configs/smoke/bird-dev.yaml
```

### 4. Normalize and Generate Mutants

```bash
BIRD_ROOT=~/data/donatello/bird uv run donatello run --config configs/smoke/bird-dev-mutate.yaml
```

### 5. Full BIRD Normalization

```bash
BIRD_ROOT=~/data/donatello/bird uv run donatello run --config configs/experiments/bird-full.yaml
```

`configs/experiments/bird-full.yaml` enables normalization only. To run the full
end-to-end pipeline with VeriEQL, SQLite execution, SQLDriller counterexample
generation, augmentation, and reporting, use:

```bash
BIRD_ROOT=~/data/donatello/bird \
VERIEQL_ROOT=~/tools/VeriEQL \
VERIEQL_PYTHON=~/venvs/verieql/bin/python \
SQLDRILLER_ROOT=~/tools/SQLDriller \
SQLDRILLER_PYTHON=~/venvs/sqldriller/bin/python \
uv run donatello run --config configs/experiments/bird-full-pipeline.yaml
```

The additional `bird-full-rerun-*` configs are resume/recovery configs for
specific stages. They assume upstream artifacts already exist in
`outputs/bird-full`.

## Configuration

Runs are driven by YAML config files under [`configs/`](configs/).

Main fields:

- `benchmark`: `bird` or `spider2`
- `bird_mode`: `dev`, `train`, or `full`
- `data_root`: benchmark root path
- `output_dir`: artifact directory for the run
- `mutation_profile`: `canonical`
- `workers`: concurrency used by VeriEQL, SQLite execution, and SQLDriller
  counterexample generation
- `resume` / `overwrite`: artifact reuse behavior
- `stages`: enable `normalize`, `mutate`, `equivalence`, `execute`,
  `counterexamples`, `augment`, and `report`

Included configs:

- `configs/smoke/bird-dev.yaml`: normalize BIRD dev only.
- `configs/smoke/bird-dev-mutate.yaml`: normalize BIRD dev and generate mutants.
- `configs/smoke/bird-train.yaml`: normalize BIRD train only.
- `configs/experiments/bird-full.yaml`: normalize BIRD dev+train.
- `configs/experiments/bird-full-pipeline.yaml`: full BIRD pipeline.
- `configs/experiments/bird-full-rerun-*.yaml`: stage-specific rerun/resume
  configs for an existing `outputs/bird-full` artifact directory.
- `configs/experiments/spider2-sqlite24-full.yaml`: full pipeline on the
  24-question Spider 2.0 SQLite subset.
- `configs/templates/local.yaml.example`: template for machine-local configs.

Example local config:

```yaml
experiment_name: local-bird-dev
benchmark: bird
bird_mode: dev
data_root: ~/data/donatello/bird
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
uv run donatello run --config configs/local/<name>.yaml
```

`augment` requires `mutants.jsonl`, `equivalence.jsonl`, `execution.jsonl`, and
`counterexamples.jsonl` to already exist unless those upstream stages are
enabled in the same run. `report` can run on partial outputs and marks missing
sections as unavailable.

## Artifacts

Each run writes to the configured `output_dir`, commonly under `outputs/`.

Artifact layout:

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

Stage behavior:

- normalization writes `normalized_examples.jsonl`
- mutation generation reads `normalized_examples.jsonl` and writes `mutants.jsonl`
- equivalence filtering reads `normalized_examples.jsonl` and `mutants.jsonl`,
  runs VeriEQL, and writes `equivalence.jsonl`
- SQLite execution reads `mutants.jsonl` and `equivalence.jsonl`, runs only
  `proven_non_equivalent` mutants, applies `timeouts.sqlite_seconds` per query,
  and writes `execution.jsonl`
- counterexample generation reads `mutants.jsonl`, `equivalence.jsonl`, and
  `execution.jsonl`, runs only `survived` plus `proven_non_equivalent` mutants,
  uses SQLDriller `exec` mode, writes `counterexamples.jsonl`, and stores
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

`workers` applies to:

- VeriEQL batch checking
- SQLite execution for retained mutants
- SQLDriller counterexample generation for retained survivors

## Comparison Utilities

The `scripts/` directory includes standalone utilities used for paper
comparisons with SpotIT-style prediction artifacts. These scripts are not part
of the core Donatello pipeline, but they are useful for reproducing model-level
EX-drop and transfer metrics.

Expected external inputs:

- SpotIT supplementary BIRD metadata under `BIRD_benchmarks/`
- SpotIT supplementary model predictions under `generated_sqls/`
- local BIRD dev SQLite databases
- Donatello `counterexamples.jsonl` and generated CE SQLite files
- optionally an official SpotIt+ CE manifest for combined standalone transfer

Scripts:

- `scripts/evaluate_spotit_on_augmented_bird.py`: evaluates the same model
  predictions before and after Donatello benchmark augmentation.
- `scripts/evaluate_ce_transfer.py`: evaluates Donatello CEs in standalone
  SpotIT-style transfer mode, excluding the configured source model and
  requiring exactly 9 target models.
- `scripts/evaluate_combined_ce_transfer.py`: evaluates the union of official
  SpotIt+ standalone CEs and Donatello standalone CEs.

The transfer scripts intentionally fail if CE database paths are missing or if
the model set does not match the expected 10-model setup. This is preferable to
silently producing undercounted paper metrics.

Typical usage:

```bash
python scripts/evaluate_spotit_on_augmented_bird.py \
  --supplementary-root /path/to/spotit-artifacts \
  --bird-db-root /path/to/bird/dev/dev_databases \
  --augmentation-manifest outputs/bird-full/augmented_databases/augmentation.jsonl \
  --output-dir outputs/spotit_donatello_transfer

python scripts/evaluate_ce_transfer.py \
  --supplementary-root /path/to/spotit-artifacts \
  --bird-db-root /path/to/bird/dev/dev_databases \
  --baseline-jsonl outputs/spotit_donatello_transfer/baseline_ex.jsonl \
  --counterexamples-jsonl outputs/bird-full/counterexamples.jsonl \
  --output-dir outputs/donatello_ce_transfer
```

## Mutation Profile

Donatello ships one canonical mutation profile with 19 rules:

- Set A: `a_null_pred`, `a_topk_ties`, `a_agg_edge`, `a_pred_loc`, `a_subq_corr_drop`
- Set B: `b_join_miss`, `b_from_unused_tv`, `b_subq_scope_typo`, `b_like_nowildcard`, `b_wildcard_wrong_op`, `b_domain_mismatch_comp`
- Set C: `c_pred_ror`, `c_pred_lcr`, `c_multi_dist`, `c_multi_setop`, `c_group_agg`, `c_group_key`, `c_bin_join`, `c_sub_qual`

Each rule either:

- emits one or more SQLGlot-validated mutants, or
- records a clean skip reason such as `rule_not_applicable`, `duplicate_sql`, or `same_as_gold`

The generator keeps at most `3` generated mutants per rule for each
gold SQL. That default is centralized in
`src/donatello/mutation/generator.py` as
`DEFAULT_MAX_GENERATED_MUTANTS_PER_RULE`, and `generate_mutations(...)` also
accepts `max_generated_per_rule=...` if you want to override it in code.

## CLI

```bash
uv run donatello --help
uv run donatello check-env
uv run donatello config validate --config <config.yaml>
uv run donatello run --config <config.yaml>
uv run donatello run --config <config.yaml> --dry-run
```

## Development

```bash
uv sync
uv run ruff check .
uv run ruff format .
uv run ty check
uv run pytest
```
