#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Configuration
# ==============================================================================
PROJECT_ROOT="/home/billt/jboa-1"
SRC_DIR="${PROJECT_ROOT}/src"
ENTRY_SCRIPT="${SRC_DIR}/multiStreamRSI.py"
REQ_FILE="${PROJECT_ROOT}/requirements.txt"

BUILD_VENV="${PROJECT_ROOT}/.build_venv"
DIST_DIR="${PROJECT_ROOT}/dist"
BINARY_NAME="multiStreamRSI"
IMAGE_NAME="multistream-rsi"
IMAGE_TAG="9.8"

# ==============================================================================
# 1. Pre-flight Checks
# ==============================================================================
echo "==> [1/6] Running pre-flight checks..."
if [[ ! -f "${ENTRY_SCRIPT}" ]]; then
    echo "ERROR: Entry script not found at ${ENTRY_SCRIPT}" >&2
    exit 1
fi

if ! command -v podman >/dev/null 2>&1; then
    echo "ERROR: podman is not installed. Run: sudo dnf install -y podman" >&2
    exit 1
fi

if ! command -v patchelf >/dev/null 2>&1; then
    echo "ERROR: patchelf is not installed. Run: sudo dnf install -y patchelf" >&2
    exit 1
fi

# ==============================================================================
# 2. Set Up Isolated Build Virtual Environment
# ==============================================================================
echo "==> [2/6] Preparing clean build virtual environment..."
if [[ ! -d "${BUILD_VENV}" ]]; then
    python3 -m venv "${BUILD_VENV}"
fi

# shellcheck disable=SC1091
source "${BUILD_VENV}/bin/activate"

pip install --upgrade pip wheel setuptools >/dev/null

if [[ -f "${REQ_FILE}" ]]; then
    echo "    Installing runtime dependencies from ${REQ_FILE}..."
    pip install -r "${REQ_FILE}"
elif [[ -f "${SRC_DIR}/requirements.txt" ]]; then
    echo "    Installing runtime dependencies from ${SRC_DIR}/requirements.txt..."
    pip install -r "${SRC_DIR}/requirements.txt"
else
    echo "ERROR: requirements.txt not found in ${PROJECT_ROOT} or ${SRC_DIR}" >&2
    exit 1
fi

# Install compile-time Nuitka dependencies (kept separate from requirements.txt)
echo "    Installing compile-time build tools (nuitka, ordered-set, zstandard)..."
pip install nuitka ordered-set zstandard >/dev/null

# ==============================================================================
# 3. Verify All Runtime & Local Imports Before Compiling
# ==============================================================================
echo "==> [3/6] Verifying all required imports in build environment..."
PYTHONPATH="${SRC_DIR}" python -c "
import flask
import werkzeug.serving
import alpaca.trading.client
import alpaca.data.historical
import pandas
import numpy
import yfinance
import pandas_market_calendars
import cryptography
import lib.utils
print('    All runtime and local imports verified successfully.')
"

# ==============================================================================
# 4. Compile Python Script into Standalone RHEL Binary via Nuitka
# ==============================================================================
echo "==> [4/6] Compiling ${ENTRY_SCRIPT} with Nuitka (this may take several minutes)..."
rm -rf "${DIST_DIR}"
mkdir -p "${DIST_DIR}"

cd "${SRC_DIR}"

python -m nuitka \
  --onefile \
  --remove-output \
  --assume-yes-for-downloads \
  --output-dir="${DIST_DIR}" \
  --output-filename="${BINARY_NAME}" \
  --include-package=lib \
  --include-package-data=certifi \
  --include-package-data=tzdata \
  --include-package-data=pytz \
  --include-package-data=curl_cffi \
  --include-package-data=pandas_market_calendars \
  "${ENTRY_SCRIPT}"

chmod 755 "${DIST_DIR}/${BINARY_NAME}"
echo "    Binary created at: ${DIST_DIR}/${BINARY_NAME}"

if [[ -f "${SRC_DIR}/parms.ini" ]]; then
    cp "${SRC_DIR}/parms.ini" "${DIST_DIR}/parms.ini"
elif [[ -f "${PROJECT_ROOT}/parms.ini" ]]; then
    cp "${PROJECT_ROOT}/parms.ini" "${DIST_DIR}/parms.ini"
fi

# ==============================================================================
# 5. Generate Containerfile Pinned to RHEL 9.8 (UBI 9.8) Running as `jboa`
# ==============================================================================
echo "==> [5/6] Generating Containerfile (RHEL 9.8)..."
cat <<'EOF' > "${DIST_DIR}/Containerfile"
# Pin explicitly to Red Hat Universal Base Image 9.8 (RHEL 9.8)
FROM registry.access.redhat.com/ubi9/ubi-minimal:9.8

# Install shadow-utils (for groupadd/useradd), CA certificates, and timezone data
RUN microdnf install -y --setopt=install_weak_deps=0 shadow-utils ca-certificates tzdata && \
    microdnf clean all

# Create non-root group and user 'jboa' with home directory /home/jboa
RUN groupadd --gid 1001 jboa && \
    useradd --uid 1001 --gid 1001 --create-home --home-dir /home/jboa --shell /bin/bash jboa

# Create /home/jboa/app/src (for parms.ini) and /home/jboa/app/logs (for log output)
WORKDIR /home/jboa/app
RUN mkdir -p /home/jboa/app/src /home/jboa/app/logs && \
    chown -R jboa:jboa /home/jboa

# Copy compiled standalone binary into /home/jboa/app owned by jboa
COPY --chown=jboa:jboa multiStreamRSI /home/jboa/app/multiStreamRSI
RUN chmod 0755 /home/jboa/app/multiStreamRSI

# Switch to non-root user 'jboa'
USER jboa
WORKDIR /home/jboa/app

# Document webhook/API port (5000) and UI dashboard port (5001)
EXPOSE 5000 5001

# Pass default config path matching utils.py's parent.parent log resolution
ENTRYPOINT ["/home/jboa/app/multiStreamRSI", "-c", "/home/jboa/app/src/parms.ini"]
EOF

# ==============================================================================
# 6. Build Podman Image
# ==============================================================================
echo "==> [6/6] Building Podman image '${IMAGE_NAME}:${IMAGE_TAG}'..."
cd "${DIST_DIR}"
podman build -t "${IMAGE_NAME}:${IMAGE_TAG}" -t "${IMAGE_NAME}:latest" -f Containerfile .

echo ""
echo "=============================================================================="
echo "BUILD & PACKAGING COMPLETE"
echo "  Standalone Binary : ${DIST_DIR}/${BINARY_NAME}"
echo "  Podman Image      : ${IMAGE_NAME}:${IMAGE_TAG} (RHEL 9.8 UBI)"
echo "  Container User    : jboa (UID 1001 / GID 1001)"
echo "  Exposed Ports     : 5000 5001"
echo "  Default Entrypoint: /home/jboa/app/multiStreamRSI -c /home/jboa/app/src/parms.ini"
echo "=============================================================================="
