#!/usr/bin/env bash
# pull_sequential_lb_results.sh
# ============================================================
# Pulls all results from the sequential 9-UE LB experiment
# from POWDER NFS to the local results/ran_9ue_lb/ directory.
#
# Run from your local Mac after the experiment completes:
#   bash scripts/pull_sequential_lb_results.sh
# ============================================================
set -euo pipefail

UEHOST="saish@pc808.emulab.net"
NFS_BASE="/proj/ATLANTIC-eVISION/exp/loadbalance/tmp/ran_9ue_lb"
LOCAL_OUT="results/ran_9ue_lb"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=15 -o BatchMode=yes"
SCP_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=15 -o BatchMode=yes"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

mkdir -p "$LOCAL_OUT"

log "=== Pulling sequential LB experiment results from POWDER ==="

# ── Primary results from uehost1 NFS ─────────────────────────
log "Pulling sequential_accumulation.csv..."
scp $SCP_OPTS \
    "$UEHOST:${NFS_BASE}/results/ran_9ue_lb/sequential_accumulation.csv" \
    "$LOCAL_OUT/" 2>/dev/null && log "  OK: sequential_accumulation.csv" || \
    log "  MISSING: sequential_accumulation.csv (experiment may not have run yet)"

log "Pulling sequential_lb.log..."
scp $SCP_OPTS \
    "$UEHOST:${NFS_BASE}/results/ran_9ue_lb/sequential_lb.log" \
    "$LOCAL_OUT/" 2>/dev/null && log "  OK: sequential_lb.log" || \
    log "  MISSING: sequential_lb.log"

log "Pulling sequential_lb_summary.txt..."
scp $SCP_OPTS \
    "$UEHOST:${NFS_BASE}/results/ran_9ue_lb/sequential_lb_summary.txt" \
    "$LOCAL_OUT/" 2>/dev/null && log "  OK: sequential_lb_summary.txt" || \
    log "  MISSING: sequential_lb_summary.txt"

# ── Continuous collectors (power + RAN) from gNB1/gNB2 ───────
log "Pulling collector CSVs from gNB1 (pc818)..."
for f in power_gnb1.csv ran_params_gnb1.csv ran_phy_mac_gnb1.csv; do
    scp $SCP_OPTS \
        "saish@pc818.emulab.net:${NFS_BASE}/results/${f}" \
        "$LOCAL_OUT/${f}" 2>/dev/null && log "  OK: $f" || \
        log "  MISSING: $f"
done

log "Pulling collector CSVs from gNB2 (pc802)..."
scp $SCP_OPTS \
    "saish@pc802.emulab.net:${NFS_BASE}/results/power_gnb2.csv" \
    "$LOCAL_OUT/power_gnb2.csv" 2>/dev/null && log "  OK: power_gnb2.csv" || \
    log "  MISSING: power_gnb2.csv"

# gNB2 logs are in /tmp (not NFS)
scp $SCP_OPTS \
    "saish@pc802.emulab.net:/tmp/gnb2_logs/*.log" \
    "$LOCAL_OUT/" 2>/dev/null && log "  OK: gnb2 stdout logs" || \
    log "  (no gnb2 logs yet)"

# ── UE stdout logs from uehost1 ───────────────────────────────
log "Pulling per-UE srsue gnb2 logs from uehost1..."
scp $SCP_OPTS \
    "$UEHOST:/tmp/ran_collect/ue4*_gnb2_stdout.log" \
    "$LOCAL_OUT/" 2>/dev/null && log "  OK: UE gnb2 stdout logs" || \
    log "  (no per-UE logs yet)"

# ── Show what we have ─────────────────────────────────────────
log ""
log "=== Local results/ran_9ue_lb/ contents ==="
ls -lh "$LOCAL_OUT/" | grep -v '^total' || echo "  (empty)"

# Check if accumulation CSV has data
if [ -f "$LOCAL_OUT/sequential_accumulation.csv" ]; then
    rows=$(wc -l < "$LOCAL_OUT/sequential_accumulation.csv")
    log ""
    log "sequential_accumulation.csv: $rows rows (including header)"
    log "Last 3 rows:"
    tail -3 "$LOCAL_OUT/sequential_accumulation.csv" | cut -c1-120
fi

log ""
log "=== Next step: run analysis ==="
log "  python3 scripts/analyze_9ue_lb_experiment.py"
