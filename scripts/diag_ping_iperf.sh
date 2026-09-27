#!/bin/bash
# diag_ping_iperf.sh — run on pc801 (uehost1) to diagnose why ping/iperf fails
# after srsue says "Network attach successful".
#
# Usage: bash diag_ping_iperf.sh [UE_NUM]
#   UE_NUM defaults to 1
#
# What it checks:
#   1. tun interface present in netns?
#   2. IP address assigned on tun interface?
#   3. Default route inside netns?
#   4. Can ping CORE_IP from netns?
#   5. Is iperf3 server reachable on CORE_IP:5301?
#   6. Is iperf3 server running on core?
#   7. TCP connectivity test
set -euo pipefail

N=${1:-1}
CORE_IP="10.45.0.1"
IPERF_PORT=5301
NETNS="ue${N}"
TUN="tun_srsue${N}"

log() { echo "[$(date +%H:%M:%S)] $*"; }
ok()  { echo "  ✓ $*"; }
fail(){ echo "  ✗ $*"; }
info(){ echo "  → $*"; }

log "=== Diagnosing UE${N} (netns=${NETNS} tun=${TUN}) ==="

# ── 1. Does the network namespace exist? ────────────────────────────────────
if sudo ip netns list 2>/dev/null | grep -q "^${NETNS}"; then
    ok "netns ${NETNS} exists"
else
    fail "netns ${NETNS} MISSING — create with: sudo ip netns add ${NETNS}"
    exit 1
fi

# ── 2. Does the tun interface exist inside the netns? ───────────────────────
TUN_INFO=$(sudo ip netns exec "${NETNS}" ip -br link show "${TUN}" 2>/dev/null || true)
if [ -n "${TUN_INFO}" ]; then
    ok "tun interface ${TUN} exists"
    info "${TUN_INFO}"
else
    fail "${TUN} NOT found in netns ${NETNS}"
    info "All interfaces in netns:"
    sudo ip netns exec "${NETNS}" ip -br link show 2>/dev/null || echo "    (none)"
    exit 1
fi

# ── 3. IP address on tun? ───────────────────────────────────────────────────
TUN_IP=$(sudo ip netns exec "${NETNS}" ip -4 -br addr show "${TUN}" 2>/dev/null \
    | awk '{print $3}' | cut -d/ -f1)
if [ -n "${TUN_IP}" ]; then
    ok "tun IP: ${TUN_IP}"
else
    fail "NO IP address on ${TUN} — PDN session may not have completed"
    exit 1
fi

# ── 4. Default route inside netns? ─────────────────────────────────────────
DEFROUTE=$(sudo ip netns exec "${NETNS}" ip route show default 2>/dev/null || true)
if [ -n "${DEFROUTE}" ]; then
    ok "default route: ${DEFROUTE}"
else
    fail "NO default route in ${NETNS}"
    info "Adding default route via ${TUN} now..."
    sudo ip netns exec "${NETNS}" ip route add default dev "${TUN}" && \
        ok "default route added" || fail "could not add route"
    DEFROUTE=$(sudo ip netns exec "${NETNS}" ip route show default 2>/dev/null || true)
    info "Route table now: ${DEFROUTE}"
fi

# ── 5. Ping test ────────────────────────────────────────────────────────────
log "Ping test (5 packets, timeout 2s each)..."
PING_OUT=$(sudo ip netns exec "${NETNS}" ping -c 5 -i 0.5 -W 2 "${CORE_IP}" 2>&1 || true)
echo "${PING_OUT}"
if echo "${PING_OUT}" | grep -q "0% packet loss\|1 received\|2 received\|3 received\|4 received\|5 received"; then
    ok "Ping to CORE (${CORE_IP}) WORKING"
else
    fail "Ping to CORE (${CORE_IP}) FAILED"
    info "Check: is iperf3 / Open5GS UPF running on core?"
    info "Check: is 'ogstun' interface UP on pc808 (core)?"
    info "  ssh saish@pc808.emulab.net 'ip -4 addr show ogstun; ip route | grep 10.45'"
fi

# ── 6. iperf3 server reachable? ─────────────────────────────────────────────
log "iperf3 server reachability (port ${IPERF_PORT})..."
IPERF_OUT=$(sudo ip netns exec "${NETNS}" \
    iperf3 -c "${CORE_IP}" -p "${IPERF_PORT}" -t 3 -b 1M --json 2>&1 || true)
if echo "${IPERF_OUT}" | grep -q '"bits_per_second"'; then
    ok "iperf3 to ${CORE_IP}:${IPERF_PORT} WORKING"
    echo "${IPERF_OUT}" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    bps = d['end']['sum_received']['bits_per_second']
    print(f'  DL throughput: {bps/1e6:.3f} Mbps')
except: pass
" 2>/dev/null || true
else
    fail "iperf3 to ${CORE_IP}:${IPERF_PORT} FAILED"
    info "iperf3 output: ${IPERF_OUT}"
    info "Fix: on pc808 run:  sudo iperf3 -s -B 10.45.0.1 -p 5301 -D"
    info "     or use per-UE ports: sudo iperf3 -s -B 10.45.0.1 -p 5200 -D (for UE1 → port 5201)"
fi

# ── 7. Routing table summary ─────────────────────────────────────────────────
log "Full routing table in ${NETNS}:"
sudo ip netns exec "${NETNS}" ip route show 2>/dev/null

log "=== Diagnosis complete for UE${N} ==="
