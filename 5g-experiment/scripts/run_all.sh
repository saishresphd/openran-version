#!/usr/bin/env bash
# run_all.sh — Run both 5G SA experiments and save results to CSV
# Usage: bash run_all.sh
# Requires: SSH access to all testbed nodes, iperf3 on core

set -e
RESULTS_DIR="results"
mkdir -p "$RESULTS_DIR"

echo "======================================================"
echo " 5G SA Experiment — Full Run (Experiment A + B)"
echo " $(date)"
echo "======================================================"

# --- Experiment A: srsRAN Project 25.10.0 + srsUE 23.04 ---
echo ""
echo "[EXP A] srsRAN Project 25.10.0 gNB + srsUE 23.04 (ZMQ, Band n3)"
echo "  Make sure gnb (pc802) and srsue (pc801) are running before this."
python3 5g-experiment/scripts/measure_5g.py \
  --exp A \
  --gnb-host pc802 \
  --ue-host  pc801 \
  --core-host pc808 \
  --ue-netns ue1 \
  --ue-iface tun_srsue \
  --gnb-log  /tmp/gnb1_srsran.log \
  --ue-log   /tmp/ue1_srsran.log \
  --gnb-proc gnb \
  --ver      srsran_5g_sa_25_10 \
  --out      "$RESULTS_DIR"

echo ""
echo "[EXP A] Done. Stopping srsRAN processes..."
ssh -o StrictHostKeyChecking=no saish@pc802.emulab.net 'sudo pkill -9 gnb 2>/dev/null || true'
ssh -o StrictHostKeyChecking=no saish@pc801.emulab.net 'sudo pkill -9 srsue 2>/dev/null || true'
sleep 5

# --- Experiment B: OAI 2026.w40 gNB + nrUE ---
echo ""
echo "[EXP B] OAI nr-softmodem 2026.w40 gNB + nrUE (RFsim, Band n78)"
echo "  Starting OAI gNB on pc811..."
ssh -o StrictHostKeyChecking=no saish@pc811.emulab.net \
  'sudo pkill -9 nr-softmodem 2>/dev/null; sleep 1; nohup sudo /tmp/openairinterface5g/cmake_targets/ran_build/build/nr-softmodem -O /etc/oai_gnb/gnb.sa.rfsim.conf --rfsim --log_config.global_log_options level,nocolor,time > /tmp/gnb2_oai.log 2>&1 &'
echo "  Waiting 15s for OAI gNB to be ready..."
sleep 15

echo "  Starting OAI nrUE on pc818..."
ssh -o StrictHostKeyChecking=no saish@pc818.emulab.net \
  'sudo pkill -9 nr-uesoftmodem 2>/dev/null; sleep 1; nohup sudo /tmp/openairinterface5g/cmake_targets/ran_build/build/nr-uesoftmodem -O /etc/oai_ue/ue.conf --rfsim --rfsimulator.serveraddr 10.10.1.3 --rfsimulator.serverport 4043 --log_config.global_log_options level,nocolor,time > /tmp/ue2_oai.log 2>&1 &'

python3 5g-experiment/scripts/measure_5g.py \
  --exp B \
  --gnb-host  pc811 \
  --ue-host   pc818 \
  --core-host pc808 \
  --ue-netns  "" \
  --ue-iface  oaitun_ue1 \
  --gnb-log   /tmp/gnb2_oai.log \
  --ue-log    /tmp/ue2_oai.log \
  --gnb-proc  nr-softmodem \
  --ver       oai_5g_sa_2026w40 \
  --out       "$RESULTS_DIR"

echo ""
echo "[EXP B] Done. Stopping OAI processes..."
ssh -o StrictHostKeyChecking=no saish@pc811.emulab.net 'sudo pkill -9 nr-softmodem 2>/dev/null || true'
ssh -o StrictHostKeyChecking=no saish@pc818.emulab.net 'sudo pkill -9 nr-uesoftmodem 2>/dev/null || true'

echo ""
echo "======================================================"
echo " ALL EXPERIMENTS COMPLETE"
echo " Results in: $RESULTS_DIR/"
ls -lh "$RESULTS_DIR"/
echo "======================================================"
