#!/bin/bash
# fix_netns_routes.sh — run on pc801 (uehost1)
# Adds a default route via tun_srsueN inside each netns ueN
# for all UEs that have a tun interface but no default route.
#
# This fixes the most common cause of "attach OK but ping fails":
# srsue creates the tun and assigns an IP, but doesn't install a
# default route — so packets sent from the netns bypass the tunnel.
#
# Usage: sudo bash fix_netns_routes.sh [MAX_UE]
#   MAX_UE defaults to 50

MAX=${1:-50}
FIXED=0
SKIPPED=0
ALREADY=0

log() { echo "[$(date +%H:%M:%S)] $*"; }

log "Fixing default routes in netns ue1..ue${MAX}..."

for N in $(seq 1 "${MAX}"); do
    NETNS="ue${N}"
    TUN="tun_srsue${N}"

    # Namespace must exist
    if ! ip netns list 2>/dev/null | grep -q "^${NETNS}"; then
        continue
    fi

    # tun must exist and have an IP
    TUN_IP=$(sudo ip netns exec "${NETNS}" ip -4 -br addr show "${TUN}" 2>/dev/null \
        | awk '{print $3}' | cut -d/ -f1 | head -1)
    if [ -z "${TUN_IP}" ]; then
        SKIPPED=$((SKIPPED+1))
        continue
    fi

    # Check for existing default route
    DEFROUTE=$(sudo ip netns exec "${NETNS}" ip route show default 2>/dev/null || true)
    if [ -n "${DEFROUTE}" ]; then
        ALREADY=$((ALREADY+1))
        continue
    fi

    # Add default route through the tun interface
    if sudo ip netns exec "${NETNS}" ip route add default dev "${TUN}" 2>/dev/null; then
        echo "  [ue${N}] added default route via ${TUN} (IP=${TUN_IP})"
        FIXED=$((FIXED+1))
    else
        echo "  [ue${N}] WARNING: could not add route (tun=${TUN} ip=${TUN_IP})"
    fi
done

log "Done. fixed=${FIXED}  already_had_route=${ALREADY}  no_tun_skipped=${SKIPPED}"
