# Working v22.10 Experiment Pipeline

This folder contains the complete, tested, and active working pipeline for the **srsRAN v22.10 50-UE accumulation experiment**.

## Active Components

1. **`run_v22_50ue.py`**
   - Main orchestrator running live on POWDER nodes:
     - Core: `pc808.emulab.net` (Open5GS EPC)
     - gNB: `pc802.emulab.net` (srsenb v22.10 multi-instance with GTP IP aliases `10.10.2.1-50`)
     - UE Host: `pc801.emulab.net` (srsue v22.10 multi-instance across network namespaces `ue1`..`ue50`)
   - Performs full measurement per attached UE:
     - Attachment timing + netns host route (`10.45.0.1/32` gateway fix)
     - 20-packet ping latency and loss test
     - 18 Downlink UDP iperf rates (1..10, 15..50 Mbps @ 10s each)
     - 18 Uplink UDP iperf rates (1..10, 15..50 Mbps @ 10s each)
     - Real-time gNB RAN parameter capture (`snap_gnb_ran`)
     - Server power/turbostat/RAPL snapshot (`snap_gnb_metrics`)
     - gNB metrics CSV window aggregates (`snap_gnb_agg`)
     - gNB JSON report aggregates (`snap_gnbj`)
     - UE-side PHY/MAC metrics extraction (`snap_ue_phy`)
   - Writes directly to the complete 170-column CSV schema:
     - Output: `results/ver_eval/v22_10/ue_results_50.csv`
     - Automatically syncs each completed UE row to `~/Desktop/v22_10_ue_results_50.csv`

2. **`cleanslate_and_launch_v22.py`**
   - Clean-slate utility that kills leftover srsenb/srsue/iperf3 instances, resets EPC IP pools cleanly, cleans network namespaces and TUN devices, wipes old logs, and launches `run_v22_50ue.py`.

## Running the Experiment

To launch with a clean slate:
```bash
python3 scripts/working_v22/cleanslate_and_launch_v22.py
```

To monitor live progress:
```bash
tail -f /tmp/run_v22_50ue.log
```
