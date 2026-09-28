# Working v25.10 Experiment Pipeline

This folder contains the complete pipeline for the **srsRAN v25.10 50-UE accumulation experiment**.

## Files
1. **`install_and_build_v25.sh`**: Installs dependencies and builds srsRAN (`release_25_10` / Project) with ZMQ into `/opt/srsRAN_v25`.
2. **`run_v25_50ue.py`**: Main orchestrator running UE 1..50 accumulation, 36-rate throughput, power, and RAN/PHY metrics collection into `results/ver_eval/v25_10/ue_results_50.csv` (syncing to `~/Desktop/v25_10_ue_results_50.csv`).
3. **`cleanslate_and_launch_v25.py`**: Clean-slate automation for EPC/5GC reset, netns cleaning, and background execution.

## Execution
```bash
python3 scripts/working_v25/cleanslate_and_launch_v25.py
```
