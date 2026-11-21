#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Usage: ./build_and_package.sh [rhel|windows|both]   (default: both)
# ==============================================================================
TARGET="${1:-both}"
if [[ "${TARGET}" != "rhel" && "${TARGET}" != "windows" && "${TARGET}" != "both" ]]; then
    echo "Usage: $0 [rhel|windows|both]" >&2
    exit 1
fi

# ==============================================================================
# Configuration
# ==============================================================================
PROJECT_ROOT="/home/billt/jboa-1"
SRC_DIR="${PROJECT_ROOT}/src"
ENTRY_SCRIPT="${SRC_DIR}/multiStreamRSI.py"
REQ_FILE="${PROJECT_ROOT}/requirements.txt"

BUILD_VENV="${PROJECT_ROOT}/.build_venv"
DIST_DIR="${PROJECT_ROOT}/dist"
WIN_PKG_DIR="${DIST_DIR}/windows_package"
BINARY_NAME="multiStreamRSI"
IMAGE_NAME="multistream-rsi"
IMAGE_TAG="9.8"
WIN_ARCHIVE="${WIN_PKG_DIR}/${IMAGE_NAME}-${IMAGE_TAG}-windows-amd64.tar"

# ==============================================================================
# 1. Pre-flight Checks
# ==============================================================================
echo "==> [1/7] Running pre-flight checks (Target: ${TARGET})..."
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
echo "==> [2/7] Preparing clean build virtual environment..."
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
echo "==> [3/7] Verifying all required imports in build environment..."
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
# 4. Compile Python Script into Standalone Linux Binary via Nuitka
# ==============================================================================
echo "==> [4/7] Compiling ${ENTRY_SCRIPT} with Nuitka (this may take several minutes)..."
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
echo "==> [5/7] Generating Containerfile (RHEL 9.8)..."
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
# 6. Build Container Image (linux/amd64)
# ==============================================================================
echo "==> [6/7] Building container image '${IMAGE_NAME}:${IMAGE_TAG}' (linux/amd64)..."
cd "${DIST_DIR}"
podman build --platform linux/amd64 -t "${IMAGE_NAME}:${IMAGE_TAG}" -t "${IMAGE_NAME}:latest" -f Containerfile .

# ==============================================================================
# 7. Package Windows Docker Archive (if target is 'windows' or 'both')
# ==============================================================================
if [[ "${TARGET}" == "windows" || "${TARGET}" == "both" ]]; then
    echo "==> [7/7] Packaging Docker archive for Windows..."
    mkdir -p "${WIN_PKG_DIR}/logs"

    # Export using docker-archive format so Windows Docker Desktop can load it cleanly
    podman save --format docker-archive -o "${WIN_ARCHIVE}" "${IMAGE_NAME}:${IMAGE_TAG}"

    # Copy sample/active parms.ini if available
    if [[ -f "${DIST_DIR}/parms.ini" ]]; then
        cp "${DIST_DIR}/parms.ini" "${WIN_PKG_DIR}/parms.ini"
    fi

    # Create Windows launcher script with CRLF line endings
    cat <<EOF > "${WIN_PKG_DIR}/run_windows.bat"
@echo off
setlocal
cd /d "%~dp0"

if not exist "logs" mkdir logs
if not exist "parms.ini" (
    echo ERROR: parms.ini not found in %~dp0
    echo Please place your parms.ini file in this folder before starting.
    pause
    exit /b 1
)

echo [1/3] Loading Docker image ${IMAGE_NAME}:${IMAGE_TAG}...
docker load -i "${IMAGE_NAME}-${IMAGE_TAG}-windows-amd64.tar"

echo [2/3] Removing any previous container instance...
docker rm -f multistream-rsi >nul 2>&1

echo [3/3] Starting multistream-rsi container on ports 5000 and 5001...
docker run -d ^
  --name multistream-rsi ^
  --restart unless-stopped ^
  -p 5000:5000 ^
  -p 5001:5001 ^
  -v "%cd%\parms.ini:/home/jboa/app/src/parms.ini:ro" ^
  -v "%cd%\logs:/home/jboa/app/logs" ^
  ${IMAGE_NAME}:${IMAGE_TAG}

echo.
echo Container started! Dashboard available at http://localhost:5001
docker ps --filter "name=multistream-rsi"
pause
EOF
    # Convert batch file line endings to Windows CRLF
    sed -i 's/$/\r/' "${WIN_PKG_DIR}/run_windows.bat"
else
    echo "==> [7/7] Skipping Windows archive export (TARGET=${TARGET})."
fi

echo ""
echo "=============================================================================="
echo "BUILD & PACKAGING COMPLETE (Target: ${TARGET})"
echo "  Standalone Binary : ${DIST_DIR}/${BINARY_NAME}"
if [[ "${TARGET}" == "rhel" || "${TARGET}" == "both" ]]; then
    echo "  RHEL Podman Image : ${IMAGE_NAME}:${IMAGE_TAG} (local Podman storage)"
fi
if [[ "${TARGET}" == "windows" || "${TARGET}" == "both" ]]; then
    echo "  Windows Bundle    : ${WIN_PKG_DIR}/"
    echo "    - Image Archive : ${WIN_ARCHIVE}"
    echo "    - Launcher      : ${WIN_PKG_DIR}/run_windows.bat"
fi
echo "  Container User    : jboa (UID 1001 / GID 1001)"
echo "  Exposed Ports     : 5000 5001"
echo "=============================================================================="
