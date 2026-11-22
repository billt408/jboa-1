#!/usr/bin/env bash
# Manage and run 2 concurrent JBOA-RSI instances from one terminal window:
#   1) user=billt | root=/home/billt/jboa-1/src | ports=5000/5001 | provider=alpaca   (READ-ONLY  NEVER modified)
#   2) user=jboa  | root=/home/jboa             | ports=6000/6001 | provider=tastytrade
#
# Usage:
#   ./sh/run_dual_instances.sh                       # bootstrap instance 2 (if needed) + run both foreground
#   ./sh/run_dual_instances.sh --setup-only          # only create/sync /home/jboa without starting
#   ./sh/run_dual_instances.sh --run-only [--bg]     # run both (foreground, or detached with --bg)
#   ./sh/run_dual_instances.sh --run1 [--bg]         # run ONLY instance 1 (billt, 5000/5001)
#   ./sh/run_dual_instances.sh --run2 [--bg]         # run ONLY instance 2 (jboa, 6000/6001)
#   ./sh/run_dual_instances.sh --stop1               # stop ONLY instance 1
#   ./sh/run_dual_instances.sh --stop2               # stop ONLY instance 2
#   ./sh/run_dual_instances.sh --stop-all            # stop both instances
#   ./sh/run_dual_instances.sh --status              # show running status, PIDs, ports & logs
#   ./sh/run_dual_instances.sh --clean2 [--yes]      # remove all instance 2 artifacts under /home/jboa
#   Add --skip-firewall-check to bypass firewalld dashboard-port validation.

set -euo pipefail

# --- Instance 1 (existing Alpaca config  NEVER modified or cleaned by this script) ---
readonly INST1_USER="billt"
readonly INST1_DIR="/home/billt/jboa-1/src"
readonly INST1_WEBHOOK_PORT=5000
readonly INST1_DASH_PORT=5001
readonly INST1_PID_FILE="/tmp/jboa-dual-inst1.pid"
readonly INST1_BG_LOG="/tmp/jboa-dual-inst1.log"

# --- Instance 2 (Tastytrade config) ---
readonly INST2_USER="jboa"
readonly INST2_DIR="/home/jboa"
readonly INST2_VENV="${INST2_DIR}/.venv"
readonly INST2_WEBHOOK_PORT=6000
readonly INST2_DASH_PORT=6001
readonly INST2_PID_FILE="/tmp/jboa-dual-inst2.pid"
readonly INST2_BG_LOG="/tmp/jboa-dual-inst2.log"

MODE="all"
BG_MODE=0
AUTO_YES=0
SKIP_FIREWALL_CHECK=0

for arg in "$@"; do
    case "${arg}" in
        --setup-only)          MODE="setup" ;;
        --run-only)            MODE="run" ;;
        --run1)                MODE="run1" ;;
        --run2)                MODE="run2" ;;
        --stop1)               MODE="stop1" ;;
        --stop2)               MODE="stop2" ;;
        --stop-all)            MODE="stop_all" ;;
        --status)              MODE="status" ;;
        --clean2)              MODE="clean2" ;;
        --bg|--daemon)         BG_MODE=1 ;;
        --yes|-y)              AUTO_YES=1 ;;
        --skip-firewall-check) SKIP_FIREWALL_CHECK=1 ;;
        -h|--help)
            sed -n '2,18p' "$0"
            exit 0
            ;;
        *)
            echo "ERROR: Unknown option '${arg}'." >&2
            echo "Usage: $0 [--setup-only|--run-only|--run1|--run2|--stop1|--stop2|--stop-all|--status|--clean2] [--bg] [--yes] [--skip-firewall-check]" >&2
            exit 2
            ;;
    esac
done

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# Auto-detect Instance 1 venv (read-only lookup)
detect_inst1_venv() {
    if [[ -x "${INST1_DIR}/.venv/bin/python" ]]; then
        echo "${INST1_DIR}/.venv"
    elif [[ -x "$(dirname "${INST1_DIR}")/.venv/bin/python" ]]; then
        echo "$(dirname "${INST1_DIR}")/.venv"
    else
        echo "ERROR: Could not find .venv for ${INST1_USER} under ${INST1_DIR} or $(dirname "${INST1_DIR}")." >&2
        exit 1
    fi
}

SUDO=""
ensure_sudo() {
    if [[ "${EUID}" -ne 0 ]]; then
        sudo -v
        SUDO="sudo"
    fi
}

port_is_listening() {
    local p="$1"
    ss -ltn "( sport = :${p} )" 2>/dev/null | grep -q ":${p}\b"
}

check_ports_free() {
    local busy=0
    for p in "$@"; do
        if port_is_listening "${p}"; then
            echo "ERROR: Port ${p} is already in use. Stop the existing process/service first." >&2
            busy=1
        fi
    done
    [[ "${busy}" -eq 0 ]] || exit 1
}

# Validate that required DASHBOARD TCP ports are open in firewalld (webhook ports are excluded)
check_firewalld_ports() {
    local strict="$1" # "strict" = abort on missing ports; "warn" = warn only
    shift
    local -a ports=("$@")

    if [[ "${SKIP_FIREWALL_CHECK}" -eq 1 ]]; then
        log "Skipping firewalld port validation (--skip-firewall-check)."
        return 0
    fi
    if ! command -v firewall-cmd >/dev/null 2>&1; then
        log "firewall-cmd not found; skipping firewalld port validation."
        return 0
    fi

    ensure_sudo
    if ! ${SUDO} firewall-cmd --state >/dev/null 2>&1; then
        log "firewalld is not running; skipping firewalld port validation."
        return 0
    fi

    local -a zones=()
    while IFS= read -r z; do
        [[ -n "${z}" ]] && zones+=("${z}")
    done < <(${SUDO} firewall-cmd --get-active-zones 2>/dev/null | awk '/^[^[:space:]]/ {print $1}')

    if [[ "${#zones[@]}" -eq 0 ]]; then
        local def_zone
        def_zone="$(${SUDO} firewall-cmd --get-default-zone 2>/dev/null || true)"
        [[ -n "${def_zone}" ]] && zones+=("${def_zone}")
    fi
    if [[ "${#zones[@]}" -eq 0 ]]; then
        log "WARNING: Could not determine active/default firewalld zone; skipping firewalld check."
        return 0
    fi

    local missing_any=0
    local primary_zone="${zones[0]}"
    local -a missing_ports=()

    for p in "${ports[@]}"; do
        local is_open=0
        local open_zone=""
        for z in "${zones[@]}"; do
            local z_ports z_rich
            z_ports="$(${SUDO} firewall-cmd --zone="${z}" --list-ports 2>/dev/null || true)"
            z_rich="$(${SUDO} firewall-cmd --zone="${z}" --list-rich-rules 2>/dev/null || true)"
            if python3 - "${p}" "${z_ports}" "${z_rich}" <<'PYEOF'
import re, sys

target = int(sys.argv[1])
ports_str = sys.argv[2]
rich_str = sys.argv[3]

for tok in ports_str.split():
    if not tok.lower().endswith("/tcp"):
        continue
    spec = tok[:-4]
    if "-" in spec:
        lo, hi = spec.split("-", 1)
        if lo.isdigit() and hi.isdigit() and int(lo) <= target <= int(hi):
            sys.exit(0)
    elif spec.isdigit() and int(spec) == target:
        sys.exit(0)

for line in rich_str.splitlines():
    if 'protocol="tcp"' not in line or " accept" not in f" {line}":
        continue
    m = re.search(r'port="(\d+)(?:-(\d+))?"', line)
    if m:
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else lo
        if lo <= target <= hi:
            sys.exit(0)

sys.exit(1)
PYEOF
            then
                is_open=1
                open_zone="${z}"
                break
            fi
        done

        if [[ "${is_open}" -eq 1 ]]; then
            log "firewalld OK: dashboard port ${p}/tcp is open (zone=${open_zone})"
        else
            log "firewalld CLOSED: dashboard port ${p}/tcp is NOT open in active zone(s): ${zones[*]}"
            missing_ports+=("${p}")
            missing_any=1
        fi
    done

    if [[ "${missing_any}" -ne 0 ]]; then
        echo "" >&2
        echo "To open the missing dashboard port(s) in firewalld (zone '${primary_zone}'), run:" >&2
        for mp in "${missing_ports[@]}"; do
            echo "  sudo firewall-cmd --permanent --zone=${primary_zone} --add-port=${mp}/tcp" >&2
        done
        echo "  sudo firewall-cmd --reload" >&2
        echo "(Or pass --skip-firewall-check to bypass.)" >&2
        if [[ "${strict}" == "strict" ]]; then
            exit 1
        fi
    fi
}

# Verify a PID belongs to <user> running multiStreamRSI.py
pid_matches_instance() {
    local pid="$1"
    local expected_user="$2"
    [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
    local info
    info="$(ps -o user=,args= -p "${pid}" 2>/dev/null || true)"
    [[ -n "${info}" ]] || return 1
    local puser cmdline
    puser="$(awk '{print $1}' <<< "${info}")"
    cmdline="$(cut -d' ' -f2- <<< "${info}")"
    [[ "${puser}" == "${expected_user}" && "${cmdline}" == *"multiStreamRSI.py"* ]]
}

# Find active PID for an instance (PID file first, then pgrep fallback)
find_instance_pid() {
    local user="$1"
    local pid_file="$2"

    if [[ -f "${pid_file}" ]]; then
        local fpid
        fpid="$(tr -d '[:space:]' < "${pid_file}" 2>/dev/null || true)"
        if pid_matches_instance "${fpid}" "${user}"; then
            echo "${fpid}"
            return 0
        fi
    fi

    if id "${user}" &>/dev/null; then
        local gpid
        gpid="$(pgrep -u "${user}" -f "multiStreamRSI\.py" 2>/dev/null | head -n 1 || true)"
        if [[ -n "${gpid}" ]]; then
            echo "${gpid}"
            return 0
        fi
    fi
    return 1
}

stop_instance() {
    local label="$1"
    local user="$2"
    local pid_file="$3"

    local pid=""
    pid="$(find_instance_pid "${user}" "${pid_file}" || true)"
    if [[ -z "${pid}" ]]; then
        log "${label} (${user}) is not running."
        rm -f "${pid_file}" 2>/dev/null || ${SUDO:+${SUDO} rm -f "${pid_file}" 2>/dev/null} || true
        return 0
    fi

    local kill_prefix=""
    if [[ "$(id -un)" != "${user}" ]]; then
        ensure_sudo
        kill_prefix="${SUDO}"
    fi

    log "Stopping ${label} (${user}, PID=${pid})..."
    ${kill_prefix} kill -TERM "${pid}" 2>/dev/null || true

    local waited=0
    while [[ "${waited}" -lt 20 ]]; do
        if ! ps -p "${pid}" >/dev/null 2>&1; then
            break
        fi
        sleep 0.5
        waited=$((waited + 1))
    done

    if ps -p "${pid}" >/dev/null 2>&1; then
        log "${label} (PID=${pid}) did not exit after 10s; sending SIGKILL..."
        ${kill_prefix} kill -KILL "${pid}" 2>/dev/null || true
        sleep 0.5
    fi

    ${kill_prefix} rm -f "${pid_file}" 2>/dev/null || rm -f "${pid_file}" 2>/dev/null || true
    log "${label} (${user}) stopped."
}

show_status() {
    echo "=== JBOA-RSI Dual Instance Status ==="
    for idx in 1 2; do
        local label user dir wport dport pid_file bg_log prov
        if [[ "${idx}" -eq 1 ]]; then
            label="Instance 1"
            user="${INST1_USER}"
            dir="${INST1_DIR}"
            wport="${INST1_WEBHOOK_PORT}"
            dport="${INST1_DASH_PORT}"
            pid_file="${INST1_PID_FILE}"
            bg_log="${INST1_BG_LOG}"
            prov="alpaca"
        else
            label="Instance 2"
            user="${INST2_USER}"
            dir="${INST2_DIR}"
            wport="${INST2_WEBHOOK_PORT}"
            dport="${INST2_DASH_PORT}"
            pid_file="${INST2_PID_FILE}"
            bg_log="${INST2_BG_LOG}"
            prov="tastytrade"
        fi

        local pid state wstate dstate
        pid="$(find_instance_pid "${user}" "${pid_file}" || true)"
        if [[ -n "${pid}" ]]; then
            state="RUNNING (PID=${pid})"
        else
            state="STOPPED"
        fi
        wstate="$(port_is_listening "${wport}" && echo "LISTENING" || echo "closed")"
        dstate="$(port_is_listening "${dport}" && echo "LISTENING" || echo "closed")"

        echo ""
        echo "${label} [${user} - ${prov}]"
        echo "  Status:    ${state}"
        echo "  Root Dir:  ${dir}"
        echo "  Ports:     webhook=${wport} (${wstate}), dashboard=${dport} (${dstate})"
        echo "  Dashboard: https://localhost:${dport}"
        if [[ -f "${bg_log}" ]]; then
            echo "  BG Log:    ${bg_log}"
        fi
    done
    echo ""
}

setup_instance2() {
    ensure_sudo
    local inst1_venv
    inst1_venv="$(detect_inst1_venv)"

    log "Setting up Instance 2 (${INST2_USER} @ ${INST2_DIR})..."

    if ! id "${INST2_USER}" &>/dev/null; then
        log "Creating Linux user '${INST2_USER}'..."
        ${SUDO} useradd -m -d "${INST2_DIR}" -s /bin/bash "${INST2_USER}"
    fi

    ${SUDO} mkdir -p \
        "${INST2_DIR}/lib" \
        "${INST2_DIR}/logs/option_buyer" \
        "${INST2_DIR}/logs/option_seller" \
        "${INST2_DIR}/logs/optionchains" \
        "${INST2_DIR}/logs/positions" \
        "${INST2_DIR}/logs/charts" \
        "${INST2_DIR}/certs" \
        "${INST2_DIR}/docs"

    ${SUDO} cp -f "${INST1_DIR}/multiStreamRSI.py" "${INST2_DIR}/multiStreamRSI.py"
    if [[ -f "${INST1_DIR}/findthewinners.py" ]]; then
        ${SUDO} cp -f "${INST1_DIR}/findthewinners.py" "${INST2_DIR}/findthewinners.py"
    fi
    for pyfile in "${INST1_DIR}"/lib/*.py; do
        [[ -e "${pyfile}" ]] && ${SUDO} cp -f "${pyfile}" "${INST2_DIR}/lib/"
    done

    if [[ -f "${INST1_DIR}/.env" ]] && ! ${SUDO} test -f "${INST2_DIR}/.env"; then
        ${SUDO} cp "${INST1_DIR}/.env" "${INST2_DIR}/.env"
        ${SUDO} chmod 600 "${INST2_DIR}/.env"
    fi

    if ! ${SUDO} test -f "${INST2_DIR}/lib/parms.ini"; then
        if [[ ! -f "${INST1_DIR}/lib/parms.ini" ]]; then
            echo "ERROR: Source ${INST1_DIR}/lib/parms.ini not found." >&2
            exit 1
        fi
        log "Bootstrapping ${INST2_DIR}/lib/parms.ini for Tastytrade (ports ${INST2_WEBHOOK_PORT}/${INST2_DASH_PORT})..."
        ${SUDO} cp "${INST1_DIR}/lib/parms.ini" "${INST2_DIR}/lib/parms.ini"

        ${SUDO} python3 - "${INST2_DIR}/lib/parms.ini" "${INST2_USER}" "${INST2_WEBHOOK_PORT}" "${INST2_DASH_PORT}" <<'PYEOF'
import re, sys
from pathlib import Path

ini_path = Path(sys.argv[1])
owner_val = sys.argv[2]
webhook_port = sys.argv[3]
dash_port = sys.argv[4]

resolved = str(ini_path.resolve())
if not resolved.startswith("/home/jboa/"):
    raise SystemExit(f"REFUSED: unexpected parms.ini target {resolved}")

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

    ${SUDO} chown -R "${INST2_USER}:${INST2_USER}" "${INST2_DIR}"
    log "Instance 2 setup complete."

    # Validate dashboard port only (webhook port does not need firewalld)
    check_firewalld_ports "warn" "${INST2_DASH_PORT}"
}

clean_instance2() {
    ensure_sudo

    if [[ "${INST2_DIR}" != "/home/jboa" || "${INST2_DIR}" == /home/billt* ]]; then
        echo "REFUSED: INST2_DIR safety check failed (${INST2_DIR})." >&2
        exit 1
    fi

    if id "${INST2_USER}" &>/dev/null && pgrep -u "${INST2_USER}" -f "multiStreamRSI\.py" >/dev/null 2>&1; then
        echo "ERROR: Instance 2 (${INST2_USER}) is running. Run '$0 --stop2' first before --clean2." >&2
        exit 1
    fi

    if ! ${SUDO} test -d "${INST2_DIR}"; then
        log "Nothing to clean: ${INST2_DIR} does not exist."
        ${SUDO} rm -f -- "${INST2_PID_FILE}" "${INST2_BG_LOG}"
        return 0
    fi

    local -a targets=(
        "${INST2_DIR}/multiStreamRSI.py"
        "${INST2_DIR}/findthewinners.py"
        "${INST2_DIR}/lib"
        "${INST2_DIR}/logs"
        "${INST2_DIR}/certs"
        "${INST2_DIR}/.venv"
        "${INST2_DIR}/.env"
        "${INST2_DIR}/__pycache__"
    )
    while IFS= read -r -d '' lf; do
        targets+=("${lf}")
    done < <(${SUDO} find "${INST2_DIR}" -maxdepth 1 -name 'jboa.log*' -print0 2>/dev/null || true)

    local found=0
    echo "WARNING: --clean2 will permanently delete Instance 2 artifacts under ${INST2_DIR}:"
    for t in "${targets[@]}"; do
        if ${SUDO} test -e "${t}" || ${SUDO} test -L "${t}"; then
            echo "  - ${t}"
            found=1
        fi
    done
    echo "(Instance 1 under /home/billt/jboa-1 will NOT be touched.)"

    if [[ "${found}" -eq 0 ]]; then
        ${SUDO} rm -f -- "${INST2_PID_FILE}" "${INST2_BG_LOG}"
        log "No Instance 2 artifacts found under ${INST2_DIR}."
        return 0
    fi

    if [[ "${AUTO_YES}" -ne 1 ]]; then
        local reply=""
        read -r -p "Type 'CLEAN2' to confirm removal: " reply
        if [[ "${reply}" != "CLEAN2" ]]; then
            log "Aborted --clean2 (no files removed)."
            exit 0
        fi
    fi

    for t in "${targets[@]}"; do
        if ${SUDO} test -L "${t}"; then
            ${SUDO} rm -f -- "${t}"
        elif ${SUDO} test -e "${t}"; then
            local rp
            rp="$(${SUDO} realpath -- "${t}")"
            if [[ "${rp}" != /home/jboa/* ]]; then
                echo "REFUSED: Path '${rp}' is outside /home/jboa/." >&2
                exit 1
            fi
            ${SUDO} rm -rf -- "${rp}"
        fi
    done
    ${SUDO} rm -f -- "${INST2_PID_FILE}" "${INST2_BG_LOG}"

    log "Instance 2 artifacts completely removed from ${INST2_DIR}. (/home/billt/jboa-1 untouched.)"
}

start_instance1_bg() {
    local inst1_venv
    inst1_venv="$(detect_inst1_venv)"

    local existing
    existing="$(find_instance_pid "${INST1_USER}" "${INST1_PID_FILE}" || true)"
    if [[ -n "${existing}" ]]; then
        echo "ERROR: Instance 1 (${INST1_USER}) is already running (PID=${existing})." >&2
        exit 1
    fi

    check_firewalld_ports "strict" "${INST1_DASH_PORT}"
    check_ports_free "${INST1_WEBHOOK_PORT}" "${INST1_DASH_PORT}"

    log "Starting Instance 1 in background: user=${INST1_USER} dir=${INST1_DIR} webhook=${INST1_WEBHOOK_PORT} dashboard=${INST1_DASH_PORT} (Alpaca)"
    if [[ "$(id -un)" == "${INST1_USER}" ]]; then
        touch "${INST1_BG_LOG}"
        chmod 644 "${INST1_BG_LOG}"
        nohup bash -c "cd '${INST1_DIR}' && exec '${inst1_venv}/bin/python' -u ./multiStreamRSI.py" \
            >> "${INST1_BG_LOG}" 2>&1 < /dev/null &
        local pid=$!
        echo "${pid}" > "${INST1_PID_FILE}"
    else
        ensure_sudo
        ${SUDO} touch "${INST1_BG_LOG}" "${INST1_PID_FILE}"
        ${SUDO} chown "${INST1_USER}:${INST1_USER}" "${INST1_BG_LOG}" "${INST1_PID_FILE}"
        ${SUDO} chmod 644 "${INST1_BG_LOG}" "${INST1_PID_FILE}"
        ${SUDO} runuser -u "${INST1_USER}" -- bash -c \
            "cd '${INST1_DIR}' && nohup '${inst1_venv}/bin/python' -u ./multiStreamRSI.py >> '${INST1_BG_LOG}' 2>&1 < /dev/null & echo \$! > '${INST1_PID_FILE}'"
        local pid
        pid="$(tr -d '[:space:]' < "${INST1_PID_FILE}")"
    fi

    sleep 1.5
    if ! ps -p "${pid}" >/dev/null 2>&1; then
        echo "ERROR: Instance 1 exited immediately after launch. Last log lines (${INST1_BG_LOG}):" >&2
        tail -n 20 "${INST1_BG_LOG}" >&2 || true
        rm -f "${INST1_PID_FILE}" 2>/dev/null || true
        exit 1
    fi

    log "Instance 1 running in background (PID=${pid})."
    log "  Dashboard: https://localhost:${INST1_DASH_PORT}"
    log "  Log:       tail -f ${INST1_BG_LOG}"
    log "  Stop:      $0 --stop1"
}

start_instance2_bg() {
    ensure_sudo
    if ! ${SUDO} test -f "${INST2_DIR}/multiStreamRSI.py" || ! ${SUDO} test -x "${INST2_VENV}/bin/python"; then
        echo "ERROR: Instance 2 is not set up under ${INST2_DIR}. Run '$0 --setup-only' first." >&2
        exit 1
    fi

    local existing
    existing="$(find_instance_pid "${INST2_USER}" "${INST2_PID_FILE}" || true)"
    if [[ -n "${existing}" ]]; then
        echo "ERROR: Instance 2 (${INST2_USER}) is already running (PID=${existing})." >&2
        exit 1
    fi

    check_firewalld_ports "strict" "${INST2_DASH_PORT}"
    check_ports_free "${INST2_WEBHOOK_PORT}" "${INST2_DASH_PORT}"

    log "Starting Instance 2 in background: user=${INST2_USER} dir=${INST2_DIR} webhook=${INST2_WEBHOOK_PORT} dashboard=${INST2_DASH_PORT} (Tastytrade)"
    ${SUDO} touch "${INST2_BG_LOG}" "${INST2_PID_FILE}"
    ${SUDO} chown "${INST2_USER}:${INST2_USER}" "${INST2_BG_LOG}" "${INST2_PID_FILE}"
    ${SUDO} chmod 644 "${INST2_BG_LOG}" "${INST2_PID_FILE}"
    ${SUDO} runuser -u "${INST2_USER}" -- bash -c \
        "cd '${INST2_DIR}' && nohup '${INST2_VENV}/bin/python' -u ./multiStreamRSI.py >> '${INST2_BG_LOG}' 2>&1 < /dev/null & echo \$! > '${INST2_PID_FILE}'"

    local pid
    pid="$(tr -d '[:space:]' < "${INST2_PID_FILE}")"

    sleep 1.5
    if ! ps -p "${pid}" >/dev/null 2>&1; then
        echo "ERROR: Instance 2 exited immediately after launch. Last log lines (${INST2_BG_LOG}):" >&2
        tail -n 20 "${INST2_BG_LOG}" >&2 || true
        ${SUDO} rm -f "${INST2_PID_FILE}" 2>/dev/null || true
        exit 1
    fi

    log "Instance 2 running in background (PID=${pid})."
    log "  Dashboard: https://localhost:${INST2_DASH_PORT}"
    log "  Log:       tail -f ${INST2_BG_LOG}"
    log "  Stop:      $0 --stop2"
}

run_instance1() {
    if [[ "${BG_MODE}" -eq 1 ]]; then
        start_instance1_bg
        return 0
    fi

    local inst1_venv
    inst1_venv="$(detect_inst1_venv)"
    check_firewalld_ports "strict" "${INST1_DASH_PORT}"
    check_ports_free "${INST1_WEBHOOK_PORT}" "${INST1_DASH_PORT}"

    local use_runuser=0
    if [[ "$(id -un)" != "${INST1_USER}" ]]; then
        ensure_sudo
        use_runuser=1
    fi

    local pid1=""
    cleanup1() {
        echo ""
        log "Stopping Instance 1 (${INST1_USER})..."
        if [[ -n "${pid1}" ]]; then
            if [[ "${use_runuser}" -eq 1 ]]; then
                ${SUDO} kill -TERM "${pid1}" 2>/dev/null || true
            else
                kill -TERM "${pid1}" 2>/dev/null || true
            fi
            wait "${pid1}" 2>/dev/null || true
        fi
        rm -f "${INST1_PID_FILE}" 2>/dev/null || true
        log "Instance 1 stopped."
    }
    trap cleanup1 INT TERM EXIT

    log "Starting Instance 1 ONLY: user=${INST1_USER} dir=${INST1_DIR} webhook=${INST1_WEBHOOK_PORT} dashboard=${INST1_DASH_PORT} (Alpaca)"
    if [[ "${use_runuser}" -eq 1 ]]; then
        ${SUDO} runuser -u "${INST1_USER}" -- bash -c \
            "cd '${INST1_DIR}' && exec '${inst1_venv}/bin/python' -u ./multiStreamRSI.py" \
            > >(sed -u $'s/^/\033[36m[billt:5001]\033[0m /') \
            2> >(sed -u $'s/^/\033[36m[billt:5001]\033[0m /' >&2) &
    else
        (cd "${INST1_DIR}" && exec "${inst1_venv}/bin/python" -u ./multiStreamRSI.py) \
            > >(sed -u $'s/^/\033[36m[billt:5001]\033[0m /') \
            2> >(sed -u $'s/^/\033[36m[billt:5001]\033[0m /' >&2) &
    fi
    pid1=$!

    log "Instance 1 running (PID=${pid1}). Dashboard: https://localhost:${INST1_DASH_PORT}  Press Ctrl+C to stop."
    wait "${pid1}" || true
}

run_instance2() {
    if [[ "${BG_MODE}" -eq 1 ]]; then
        start_instance2_bg
        return 0
    fi

    ensure_sudo
    if ! ${SUDO} test -f "${INST2_DIR}/multiStreamRSI.py" || ! ${SUDO} test -x "${INST2_VENV}/bin/python"; then
        echo "ERROR: Instance 2 is not set up under ${INST2_DIR}. Run '$0 --setup-only' first." >&2
        exit 1
    fi
    check_firewalld_ports "strict" "${INST2_DASH_PORT}"
    check_ports_free "${INST2_WEBHOOK_PORT}" "${INST2_DASH_PORT}"

    local pid2=""
    cleanup2() {
        echo ""
        log "Stopping Instance 2 (${INST2_USER})..."
        if [[ -n "${pid2}" ]]; then
            ${SUDO} kill -TERM "${pid2}" 2>/dev/null || true
            wait "${pid2}" 2>/dev/null || true
        fi
        ${SUDO} rm -f "${INST2_PID_FILE}" 2>/dev/null || true
        log "Instance 2 stopped."
    }
    trap cleanup2 INT TERM EXIT

    log "Starting Instance 2 ONLY: user=${INST2_USER} dir=${INST2_DIR} webhook=${INST2_WEBHOOK_PORT} dashboard=${INST2_DASH_PORT} (Tastytrade)"
    ${SUDO} runuser -u "${INST2_USER}" -- bash -c \
        "cd '${INST2_DIR}' && exec '${INST2_VENV}/bin/python' -u ./multiStreamRSI.py" \
        > >(sed -u $'s/^/\033[33m[jboa:6001]\033[0m /') \
        2> >(sed -u $'s/^/\033[33m[jboa:6001]\033[0m /' >&2) &
    pid2=$!

    log "Instance 2 running (PID=${pid2}). Dashboard: https://localhost:${INST2_DASH_PORT}  Press Ctrl+C to stop."
    wait "${pid2}" || true
}

run_both() {
    if [[ "${BG_MODE}" -eq 1 ]]; then
        start_instance1_bg
        start_instance2_bg
        return 0
    fi

    ensure_sudo
    local inst1_venv
    inst1_venv="$(detect_inst1_venv)"

    if ! ${SUDO} test -f "${INST2_DIR}/multiStreamRSI.py" || ! ${SUDO} test -x "${INST2_VENV}/bin/python"; then
        echo "ERROR: Instance 2 is not set up under ${INST2_DIR}. Run '$0 --setup-only' first." >&2
        exit 1
    fi

    check_firewalld_ports "strict" "${INST1_DASH_PORT}" "${INST2_DASH_PORT}"
    check_ports_free \
        "${INST1_WEBHOOK_PORT}" "${INST1_DASH_PORT}" \
        "${INST2_WEBHOOK_PORT}" "${INST2_DASH_PORT}"

    local pid1="" pid2=""
    cleanup_both() {
        echo ""
        log "Shutting down both instances gracefully..."
        [[ -n "${pid1}" ]] && ${SUDO} kill -TERM "${pid1}" 2>/dev/null || true
        [[ -n "${pid2}" ]] && ${SUDO} kill -TERM "${pid2}" 2>/dev/null || true
        [[ -n "${pid1}" ]] && wait "${pid1}" 2>/dev/null || true
        [[ -n "${pid2}" ]] && wait "${pid2}" 2>/dev/null || true
        log "Both instances stopped."
    }
    trap cleanup_both INT TERM EXIT

    log "Starting Instance 1: user=${INST1_USER} dir=${INST1_DIR} webhook=${INST1_WEBHOOK_PORT} dashboard=${INST1_DASH_PORT} (Alpaca)"
    ${SUDO} runuser -u "${INST1_USER}" -- bash -c \
        "cd '${INST1_DIR}' && exec '${inst1_venv}/bin/python' -u ./multiStreamRSI.py" \
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

    wait -n "${pid1}" "${pid2}" || true
    log "One instance exited; stopping remaining instance..."
}

case "${MODE}" in
    setup)    setup_instance2 ;;
    run)      run_both ;;
    run1)     run_instance1 ;;
    run2)     run_instance2 ;;
    stop1)    stop_instance "Instance 1" "${INST1_USER}" "${INST1_PID_FILE}" ;;
    stop2)    stop_instance "Instance 2" "${INST2_USER}" "${INST2_PID_FILE}" ;;
    stop_all)
        stop_instance "Instance 1" "${INST1_USER}" "${INST1_PID_FILE}"
        stop_instance "Instance 2" "${INST2_USER}" "${INST2_PID_FILE}"
        ;;
    status)   show_status ;;
    clean2)   clean_instance2 ;;
    all)
        setup_instance2
        run_both
        ;;
esac