# Working v23.11 Experiment Pipeline

This folder contains the complete pipeline for the **srsRAN v23.11 50-UE accumulation experiment**.

## Files
1. **`install_and_build_v23.sh`**: Installs dependencies and builds srsRAN 4G (`release_23_11`) with ZMQ into `/opt/srsRAN_v23`.
2. **`run_v23_50ue.py`**: Main orchestrator running UE 1..50 accumulation, 36-rate throughput, power, and RAN/PHY metrics collection into `results/ver_eval/v23_11/ue_results_50.csv` (syncing to `~/Desktop/v23_11_ue_results_50.csv`).
3. **`cleanslate_and_launch_v23.py`**: Clean-slate automation for EPC reset, netns cleaning, and background execution.

## Execution
```bash
python3 scripts/working_v23/cleanslate_and_launch_v23.py
```
