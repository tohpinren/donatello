# syntax=docker/dockerfile:1.7

FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        coreutils \
    && rm -rf /var/lib/apt/lists/*

ENV VERIEQL_ROOT=/verieql
ENV VERIEQL_PYTHON=/opt/venvs/verieql/bin/python
ENV PATH="/opt/venvs/verieql/bin:${PATH}"

WORKDIR ${VERIEQL_ROOT}

COPY tools/VeriEQL ${VERIEQL_ROOT}
COPY tools/SQLDriller/third_party/ce_gen/solver/modification/main.py ${VERIEQL_ROOT}/main.py
COPY tools/SQLDriller/third_party/ce_gen/solver/modification/cli_within_timeout.py ${VERIEQL_ROOT}/parallel/cli_within_timeout.py

RUN python -m venv /opt/venvs/verieql \
    && "${VERIEQL_PYTHON}" -m pip install --upgrade pip \
    && "${VERIEQL_PYTHON}" -m pip install -r "${VERIEQL_ROOT}/requirements.txt" \
    && "${VERIEQL_PYTHON}" -m pip install "setuptools<81" \
    && "${VERIEQL_PYTHON}" -m pip install --force-reinstall "z3-solver==4.15.0.0" \
    && "${VERIEQL_PYTHON}" - <<'PY'
from pathlib import Path
import shutil
import z3

site_z3_dir = Path(z3.__file__).resolve().parent
source_dir = Path("/opt/tools/VeriEQL/z3py_libs")
if not source_dir.exists():
    source_dir = Path("/verieql/z3py_libs")
for source in source_dir.glob("*.py"):
    shutil.copy2(source, site_z3_dir / source.name)
print(f"Patched Z3 Python files in {site_z3_dir}")
PY

RUN "${VERIEQL_PYTHON}" -m test.test_env

CMD ["sleep", "infinity"]
