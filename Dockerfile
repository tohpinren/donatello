# syntax=docker/dockerfile:1.7

FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        coreutils \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

ENV VERIEQL_ROOT=/opt/tools/VeriEQL
ENV VERIEQL_PYTHON=/opt/venvs/verieql/bin/python
ENV SQLDRILLER_ROOT=/opt/tools/SQLDriller
ENV SQLDRILLER_PYTHON=/opt/venvs/sqldriller/bin/python
ENV BIRD_ROOT=/opt/donatello/data/bird
ENV DONATELLO_OUTPUT_ROOT=/opt/donatello/outputs
ENV PATH="/opt/venvs/verieql/bin:/opt/venvs/sqldriller/bin:${PATH}"

COPY tools/VeriEQL ${VERIEQL_ROOT}
COPY tools/SQLDriller ${SQLDRILLER_ROOT}

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
for source in source_dir.glob("*.py"):
    shutil.copy2(source, site_z3_dir / source.name)
print(f"Patched Z3 Python files in {site_z3_dir}")
PY

RUN python -m venv /opt/venvs/sqldriller \
    && "${SQLDRILLER_PYTHON}" -m pip install --upgrade pip \
    && "${SQLDRILLER_PYTHON}" -m pip install -r "${SQLDRILLER_ROOT}/requirements.txt"

COPY donatello /opt/donatello

WORKDIR /opt/donatello

RUN cd "${VERIEQL_ROOT}" \
    && "${VERIEQL_PYTHON}" -m test.test_env \
    && cd /opt/donatello \
    && uv sync --frozen --no-dev \
    && uv run donatello --help >/dev/null

RUN mkdir -p "${BIRD_ROOT}" "${DONATELLO_OUTPUT_ROOT}"

CMD ["uv", "run", "donatello", "--help"]
