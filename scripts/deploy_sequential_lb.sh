#!/usr/bin/env bash
# deploy_sequential_lb.sh
# ============================================================
# Deploys run_sequential_9ue_lb.py and all dependencies to the
# POWDER NFS workspace so it can be run on uehost1 (pc808).
#
# Run this from your local Mac:
#   bash scripts/deploy_sequential_lb.sh
#
# Then on uehost1:
#   python3 /proj/ATLANTIC-eVISION/exp/loadbalance/tmp/ran_9ue_lb/scripts/run_sequential_9ue_lb.py
# ============================================================
set -euo pipefail

UEHOST="saish@pc808.emulab.net"
NFS_BASE="/proj/ATLANTIC-eVISION/exp/loadbalance/tmp/ran_9ue_lb"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=15 -o BatchMode=yes"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

log "=== Deploying sequential 9-UE LB experiment to POWDER ==="

# ── 1. Ensure NFS directories exist ─────────────────────────
log "Creating NFS directories..."
ssh $SSH_OPTS "$UEHOST" "bash -s" <<"ENDSSH"
mkdir -p /proj/ATLANTIC-eVISION/exp/loadbalance/tmp/ran_9ue_lb/{scripts,results/ran_9ue_lb}
chmod 777 /proj/ATLANTIC-eVISION/exp/loadbalance/tmp/ran_9ue_lb/results/ran_9ue_lb 2>/dev/null || true
ENDSSH
log "Directories OK."

# ── 2. Copy the orchestrator script ──────────────────────────
log "Copying run_sequential_9ue_lb.py..."
scp $SSH_OPTS \
    "$(dirname "$0")/run_sequential_9ue_lb.py" \
    "$UEHOST:${NFS_BASE}/scripts/run_sequential_9ue_lb.py"

# ── 3. Copy analysis script ───────────────────────────────────
log "Copying analyze_9ue_lb_experiment.py..."
scp $SSH_OPTS \
    "$(dirname "$0")/analyze_9ue_lb_experiment.py" \
    "$UEHOST:${NFS_BASE}/scripts/analyze_9ue_lb_experiment.py"

# ── 4. Verify UE gnb2 configs are present on NFS ─────────────
log "Checking UE40-49 gnb2 configs on NFS..."
ssh $SSH_OPTS "$UEHOST" "bash -s" <<"ENDSSH"
NFS_CONF="/proj/ATLANTIC-eVISION/exp/loadbalance/tmp/ran_9ue_lb/configs/ues"
missing=0
for i in $(seq 40 49); do
    f="${NFS_CONF}/ue${i}_gnb2.conf"
    if [ ! -f "$f" ]; then
        echo "  MISSING: $f"
        missing=$((missing+1))
    else
        # Verify IMSI length = 15 digits
        imsi=$(grep -m1 'imsi' "$f" | grep -oP '\d+')
        len=${#imsi}
        if [ "$len" != "15" ]; then
            echo "  BAD IMSI ($len digits): $f  imsi=$imsi"
        else
            echo "  OK: $f  (IMSI=$imsi, len=$len)"
        fi
    fi
done
echo "Missing configs: $missing"
ENDSSH

# ── 5. Check gNB2 enb slot configs on pc802 ───────────────────
log "Checking gNB2 enb slot configs on pc802..."
ssh $SSH_OPTS "saish@pc802.emulab.net" "bash -s" <<"ENDSSH"
missing=0
for i in $(seq 40 49); do
    f="/etc/srsenb/enb_ue${i}.conf"
    [ -f "$f" ] && echo "  OK: $f" || { echo "  MISSING: $f"; missing=$((missing+1)); }
done
echo "Missing gNB2 enb configs: $missing"
ENDSSH

# ── 6. Check IP aliases on gNB2 ──────────────────────────────
log "Checking IP aliases 10.10.1.240-249 on gNB2..."
ssh $SSH_OPTS "saish@pc802.emulab.net" "ip addr | grep '10\.10\.1\.24'" || \
    log "WARNING: Some IP aliases may be missing on gNB2"

# ── 7. Check current UE count on gNB1 ────────────────────────
log "Current gNB1 UE count..."
ssh $SSH_OPTS "saish@pc818.emulab.net" "bash -s" <<"ENDSSH"
f="/proj/ATLANTIC-eVISION/exp/loadbalance/tmp/ran_9ue_lb/results/ran_params_gnb1.csv"
if [ -f "$f" ]; then
    echo "  Last ran_params row: $(tail -1 $f)"
else
    echo "  ran_params_gnb1.csv not found — check if collectors are running"
fi
ENDSSH

log "=== Deploy complete ==="
log ""
log "Next steps:"
log "  1. SSH to uehost1:"
log "       ssh saish@pc808.emulab.net"
log ""
log "  2. Run dry-run first to verify connectivity:"
log "       python3 ${NFS_BASE}/scripts/run_sequential_9ue_lb.py --dry-run"
log ""
log "  3. Run the live experiment (UE49 → UE40, one at a time):"
log "       python3 ${NFS_BASE}/scripts/run_sequential_9ue_lb.py"
log ""
log "  4. Or run a single UE test first (UE49 only):"
log "       python3 ${NFS_BASE}/scripts/run_sequential_9ue_lb.py --start-ue 49 --end-ue 49"
log ""
log "  Results will appear in:"
log "    ${NFS_BASE}/results/ran_9ue_lb/sequential_accumulation.csv"
log "    ${NFS_BASE}/results/ran_9ue_lb/sequential_lb.log"
