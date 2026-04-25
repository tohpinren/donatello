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
- resumable artifact-based runs driven by YAML config files
- `uv`-managed Python environment with Ruff, Ty, and pytest

What is planned next:

- VeriEQL equivalence filtering
- SQLite execution and mutation scoring
- SQLDriller counterexample generation
- Spider 2.0 SQLite-subset normalization
- reporting and benchmark augmentation

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
```

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
- `resume` / `overwrite`: artifact reuse behavior
- `stages`: enable `normalize` and `mutate` today

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
  equivalence: false
  execute: false
  counterexamples: false
  augment: false
  report: false
```

Use [`configs/templates/local.yaml.example`](configs/templates/local.yaml.example) as the starting point for machine-specific configs.

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
```

Current stage behavior:

- normalization writes `normalized_examples.jsonl`
- mutation generation reads `normalized_examples.jsonl` and writes `mutants.jsonl`
- `resume: true` reuses existing artifacts
- `overwrite: true` replaces them

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
