#!/usr/bin/env bash
# Set up and run 2 concurrent JBOA-RSI instances from one terminal window:
#   1) user=billt | root=/home/billt/jboa-1/src | ports=5000/5001 | provider=alpaca
#   2) user=jboa  | root=/home/jboa             | ports=6000/6001 | provider=tastytrade
#
# Usage:
#   ./run_dual_instances.sh               # bootstrap instance 2 (if needed) + run both
#   ./run_dual_instances.sh --setup-only  # only create/sync /home/jboa without starting
#   ./run_dual_instances.sh --run-only    # skip setup and start both immediately

set -euo pipefail

# --- Instance 1 (existing Alpaca config) ---
INST1_USER="billt"
INST1_DIR="/home/billt/jboa-1/src"
INST1_WEBHOOK_PORT=5000
INST1_DASH_PORT=5001

# Auto-detect Instance 1 venv (checks src/.venv then parent .venv)
if [[ -x "${INST1_DIR}/.venv/bin/python" ]]; then
    INST1_VENV="${INST1_DIR}/.venv"
elif [[ -x "$(dirname "${INST1_DIR}")/.venv/bin/python" ]]; then
    INST1_VENV="$(dirname "${INST1_DIR}")/.venv"
else
    echo "ERROR: Could not find .venv for ${INST1_USER} under ${INST1_DIR} or $(dirname "${INST1_DIR}")." >&2
    exit 1
fi

# --- Instance 2 (Tastytrade config) ---
INST2_USER="jboa"
INST2_DIR="/home/jboa"
INST2_VENV="${INST2_DIR}/.venv"
INST2_WEBHOOK_PORT=6000
INST2_DASH_PORT=6001

MODE="all"
case "${1:-}" in
    --setup-only) MODE="setup" ;;
    --run-only)   MODE="run" ;;
    "" )          MODE="all" ;;
    *)
        echo "Usage: $0 [--setup-only|--run-only]" >&2
        exit 2
        ;;
esac

# Elevate once upfront (needed to create/chown /home/jboa and launch as both users)
if [[ "${EUID}" -ne 0 ]]; then
    sudo -v
    SUDO="sudo"
else
    SUDO=""
fi

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

setup_instance2() {
    log "Setting up Instance 2 (${INST2_USER} @ ${INST2_DIR})..."

    # 1. Create jboa user & home directory if missing
    if ! id "${INST2_USER}" &>/dev/null; then
        log "Creating Linux user '${INST2_USER}'..."
        ${SUDO} useradd -m -d "${INST2_DIR}" -s /bin/bash "${INST2_USER}"
    fi

    # 2. Ensure directory structure exists
    ${SUDO} mkdir -p \
        "${INST2_DIR}/lib" \
        "${INST2_DIR}/logs/option_buyer" \
        "${INST2_DIR}/logs/option_seller" \
        "${INST2_DIR}/logs/optionchains" \
        "${INST2_DIR}/logs/positions" \
        "${INST2_DIR}/logs/charts" \
        "${INST2_DIR}/certs"

    # 3. Copy code as REAL files (never symlinks, since parms.ini resolves via __file__.resolve())
    ${SUDO} cp -f "${INST1_DIR}/multiStreamRSI.py" "${INST2_DIR}/multiStreamRSI.py"
    if [[ -f "${INST1_DIR}/findthewinners.py" ]]; then
        ${SUDO} cp -f "${INST1_DIR}/findthewinners.py" "${INST2_DIR}/findthewinners.py"
    fi
    for pyfile in "${INST1_DIR}"/lib/*.py; do
        [[ -e "${pyfile}" ]] && ${SUDO} cp -f "${pyfile}" "${INST2_DIR}/lib/"
    done

    # 4. Copy .env if present and not yet created in /home/jboa
    if [[ -f "${INST1_DIR}/.env" && ! -f "${INST2_DIR}/.env" ]]; then
        ${SUDO} cp "${INST1_DIR}/.env" "${INST2_DIR}/.env"
        ${SUDO} chmod 600 "${INST2_DIR}/.env"
    fi

    # 5. Bootstrap /home/jboa/lib/parms.ini ONLY if missing (never clobber manual edits)
    if [[ ! -f "${INST2_DIR}/lib/parms.ini" ]]; then
        if [[ ! -f "${INST1_DIR}/lib/parms.ini" ]]; then
            echo "ERROR: Source ${INST1_DIR}/lib/parms.ini not found." >&2
            exit 1
        fi
        log "Bootstrapping ${INST2_DIR}/lib/parms.ini for Tastytrade (ports ${INST2_WEBHOOK_PORT}/${INST2_DASH_PORT})..."
        ${SUDO} cp "${INST1_DIR}/lib/parms.ini" "${INST2_DIR}/lib/parms.ini"

        # Safely update INI keys in-place while preserving JSON sections & comments
        ${SUDO} python3 - "${INST2_DIR}/lib/parms.ini" "${INST2_USER}" "${INST2_WEBHOOK_PORT}" "${INST2_DASH_PORT}" <<'PYEOF'
import re, sys
from pathlib import Path

ini_path = Path(sys.argv[1])
owner_val = sys.argv[2]
webhook_port = sys.argv[3]
dash_port = sys.argv[4]

text = ini_path.read_text(encoding="utf-8")

def set_ini_key(raw: str, section: str, key: str, value: str) -> str:
    lines = raw.splitlines(keepends=True)
    sec_re = re.compile(r"^\s*\[([^\]]+)\]\s*(?:[;#].*)?$")
    key_re = re.compile(rf"^(\s*){re.escape(key)}(\s*=).*$", re.IGNORECASE)

    sec_start = None
    sec_end = len(lines)
    for i, line in enumerate(lines):
        m = sec_re.match(line)
        if m:
            name = m.group(1).strip().lower()
            if sec_start is None and name == section.lower():
                sec_start = i
            elif sec_start is not None:
                sec_end = i
                break

    new_line = f"{key} = {value}\n"
    if sec_start is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"\n[{section}]\n{new_line}")
        return "".join(lines)

    for i in range(sec_start + 1, sec_end):
        if key_re.match(lines[i]):
            lines[i] = new_line
            return "".join(lines)

    insert_at = sec_end
    if insert_at > 0 and not lines[insert_at - 1].endswith("\n"):
        lines[insert_at - 1] += "\n"
    lines.insert(insert_at, new_line)
    return "".join(lines)

updates = [
    ("general", "owner", owner_val),
    ("providers", "ALPACA.enabled", "false"),
    ("providers", "TASTYTRADE.enabled", "true"),
    ("option_trading", "optionchainprovider", "tastytrade"),
    ("option_trading", "transactionprovider", "tastytrade"),
    ("webhook", "host", "127.0.0.1"),
    ("webhook", "port", webhook_port),
    ("dashboard", "host", "0.0.0.0"),
    ("dashboard", "port", dash_port),
]
for sec, k, v in updates:
    text = set_ini_key(text, sec, k, v)

ini_path.write_text(text, encoding="utf-8")
PYEOF
        ${SUDO} chmod 600 "${INST2_DIR}/lib/parms.ini"
        log "Created ${INST2_DIR}/lib/parms.ini  verify [tastytrade_api] credentials before live trading."
    else
        log "Keeping existing ${INST2_DIR}/lib/parms.ini (already present)."
    fi

# 6. Create (or rebuild if Python version / packages are incomplete) dedicated virtualenv matching Instance 1's exact Python binary
    local inst1_py_ver inst1_base_py inst2_py_ver=""
    inst1_py_ver="$("${inst1_venv}/bin/python" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
    inst1_base_py="$("${inst1_venv}/bin/python" -c 'import os, sys
for cand in (os.path.realpath(sys.executable), getattr(sys, "_base_executable", "")):
    if cand and os.path.exists(cand) and not cand.startswith("/home/billt"):
        print(cand)
        break
')"
    if [[ -z "${inst1_base_py}" || ! -x "${inst1_base_py}" ]]; then
        inst1_base_py="$(command -v "python${inst1_py_ver}" || true)"
    fi
    if [[ -z "${inst1_base_py}" || ! -x "${inst1_base_py}" ]]; then
        echo "ERROR: Could not locate system Python ${inst1_py_ver} interpreter to build ${INST2_VENV}." >&2
        exit 1
    fi

    if ${SUDO} test -x "${INST2_VENV}/bin/python"; then
        inst2_py_ver="$(${SUDO} "${INST2_VENV}/bin/python" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || true)"
    fi

    if [[ "${inst2_py_ver}" != "${inst1_py_ver}" ]] || ! ${SUDO} "${INST2_VENV}/bin/python" -c "import flask, aiohttp" >/dev/null 2>&1; then
        if ${SUDO} test -d "${INST2_VENV}" && [[ "${INST2_VENV}" == "/home/jboa/.venv" ]]; then
            log "Removing incomplete/mismatched virtualenv at ${INST2_VENV} (found Python ${inst2_py_ver:-none}, need ${inst1_py_ver})..."
            ${SUDO} rm -rf -- "${INST2_VENV}"
        fi
        log "Creating Python ${inst1_py_ver} virtualenv at ${INST2_VENV} using ${inst1_base_py}..."
        local req_tmp
        req_tmp="$(mktemp)"
        "${inst1_venv}/bin/pip" freeze > "${req_tmp}"
        ${SUDO} "${inst1_base_py}" -m venv "${INST2_VENV}"
        ${SUDO} "${INST2_VENV}/bin/pip" install --upgrade pip >/dev/null
        ${SUDO} "${INST2_VENV}/bin/pip" install -r "${req_tmp}"
        rm -f "${req_tmp}"
    fi
    
    # 7. Ensure jboa owns its entire root directory
    ${SUDO} chown -R "${INST2_USER}:${INST2_USER}" "${INST2_DIR}"
    log "Instance 2 setup complete."
}

check_ports_free() {
    local busy=0
    for p in "${INST1_WEBHOOK_PORT}" "${INST1_DASH_PORT}" "${INST2_WEBHOOK_PORT}" "${INST2_DASH_PORT}"; do
        if ss -ltn "( sport = :${p} )" 2>/dev/null | grep -q ":${p}\b"; then
            echo "ERROR: Port ${p} is already in use. Stop the existing service first (e.g., sudo systemctl stop jboa-rsi.service)." >&2
            busy=1
        fi
    done
    [[ "${busy}" -eq 0 ]] || exit 1
}

run_both() {
    check_ports_free

    local pid1="" pid2=""
    cleanup() {
        echo ""
        log "Shutting down both instances gracefully..."
        [[ -n "${pid1}" ]] && ${SUDO} kill -TERM "${pid1}" 2>/dev/null || true
        [[ -n "${pid2}" ]] && ${SUDO} kill -TERM "${pid2}" 2>/dev/null || true
        wait "${pid1:-}" 2>/dev/null || true
        wait "${pid2:-}" 2>/dev/null || true
        log "Both instances stopped."
    }
    trap cleanup INT TERM EXIT

    log "Starting Instance 1: user=${INST1_USER} dir=${INST1_DIR} webhook=${INST1_WEBHOOK_PORT} dashboard=${INST1_DASH_PORT} (Alpaca)"
    ${SUDO} runuser -u "${INST1_USER}" -- bash -c \
        "cd '${INST1_DIR}' && exec '${INST1_VENV}/bin/python' -u ./multiStreamRSI.py" \
        > >(sed -u $'s/^/\033[36m[billt:5001]\033[0m /') \
        2> >(sed -u $'s/^/\033[36m[billt:5001]\033[0m /' >&2) &
    pid1=$!

    log "Starting Instance 2: user=${INST2_USER} dir=${INST2_DIR} webhook=${INST2_WEBHOOK_PORT} dashboard=${INST2_DASH_PORT} (Tastytrade)"
    ${SUDO} runuser -u "${INST2_USER}" -- bash -c \
        "cd '${INST2_DIR}' && exec '${INST2_VENV}/bin/python' -u ./multiStreamRSI.py" \
        > >(sed -u $'s/^/\033[33m[jboa:6001]\033[0m /') \
        2> >(sed -u $'s/^/\033[33m[jboa:6001]\033[0m /' >&2) &
    pid2=$!

    log "Both instances running (PIDs: billt=${pid1}, jboa=${pid2}). Press Ctrl+C to stop both."
    log "  Dashboard 1 (Alpaca):     https://localhost:${INST1_DASH_PORT}"
    log "  Dashboard 2 (Tastytrade): https://localhost:${INST2_DASH_PORT}"

    # Wait until either exits (or Ctrl+C triggers cleanup)
    wait -n "${pid1}" "${pid2}" || true
    log "One instance exited; stopping remaining instance..."
}

if [[ "${MODE}" == "setup" || "${MODE}" == "all" ]]; then
    setup_instance2
fi
if [[ "${MODE}" == "run" || "${MODE}" == "all" ]]; then
    run_both
fi