# OpenAirInterface (OAI) 4G LTE 50-UE Evaluation & Benchmarking Suite

This directory contains the complete end-to-end automation suite for deploying, configuring, compiling, executing, and collecting datasets for **OpenAirInterface (OAI) 4G LTE** on the POWDER testbed nodes (`pc808` Core/EPC, `pc802` eNB host, `pc801` UE host), maintaining 100% schema parity (177 metrics) with srsRAN v22.10, v23.11, and v25.10 benchmarks.

---

## Architecture & Topology

| Node Name | Node Hostname / IP | Role / Component |
|:---|:---|:---|
| **Control Host** | Local Workstation / Laptop | Orchestrates scripts via SSH key `~/.ssh/id_ed25519` |
| **Core Node** | `pc808.emulab.net` (`10.10.1.1`) | Open5GS EPC (MME, SGW-C, SGW-U, SMF, UPF) + iperf3 server |
| **eNB Host** | `pc802.emulab.net` (`10.10.1.2`) | OAI 4G `lte-softmodem` (Band 7, 50 PRB / 10 MHz, RFsimulator TCP server) |
| **UE Host** | `pc801.emulab.net` (`10.10.1.4`) | OAI 4G `lte-uesoftmodem` (50 UEs in network namespaces `ue1`..`ue50`) |

---

## Step-by-Step Execution Guide

> **Note on Execution Location:**
> All commands below are designed to be run from the **root directory of the `openran-version` repository on your local Control Host** (`/Users/.../playground/` or repository root).
> The orchestration scripts connect over SSH to `pc808`, `pc802`, and `pc801` automatically.

---

### Step 1: Install Dependencies, Apply Patches & Build OAI
**Where to run:** On the **Control Host** (repository root)
```bash
bash scripts/working_oai/install_and_build_oai.sh
```
**What this does automatically:**
1. Connects to `pc802` (eNB) and `pc801` (UE) over SSH.
2. Installs all required OS and build toolchains (`cmake`, `ninja-build`, `libboost-all-dev`, `libzmq3-dev`, `libsctp-dev`, `ccache`, `libconfig-dev`, `libatlas-base-dev`).
3. Clones OpenAirInterface at `/opt/openairinterface5g` and checks out the stable build tag.
4. **Applies eNB MBMS Patch on `pc802`**: Disables unconditional `ENB_NAS_USE_TUN_W_MBMS_BIT` in `executables/lte-softmodem.c` to avoid device contention on `oaitun_enm1` during multi-instance execution.
5. **Applies UE NAS TAI Patch on `pc801`**: Updates `openair3/NAS/UE/EMM/SAP/emm_recv.c` to accept Open5GS non-consecutive TAC Attach Accept lists (`cause=99` fix).
6. Compiles `lte-softmodem` (on `pc802`) and `lte-uesoftmodem` (on `pc801`) with RFsimulator support.

---

### Step 2: Generate & Deploy 50 eNB and 50 UE Configurations
**Where to run:** On the **Control Host** (repository root)
```bash
python3 scripts/working_oai/gen_configs_oai.py
```
**Generated artifacts:**
- **eNodeB Confs** on `pc802` (`/etc/oai_enb/enb_ue1.conf` ... `/etc/oai_enb/enb_ue50.conf`):
  - RFsimulator ports: `40010`, `40020`, ... `40500`.
  - GTP-U IP Aliases: `10.10.2.1/24` ... `10.10.2.50/24`.
- **UE Confs & NVRAM** on `pc801` (`/tmp/oai_ue_ctx/ue1/` ... `/tmp/oai_ue_ctx/ue50/`):
  - Matching IMSIs `999700000000001` .. `999700000000050` with OPc & K keys.

---

### Step 3: Launch Clean-Slate 50-UE Experiment
**Where to run:** On the **Control Host** (repository root)
```bash
python3 scripts/working_oai/cleanslate_and_launch_oai.py
```
**What this does automatically:**
- Kills any stale processes on `pc802`, `pc801`, and `pc808`.
- Restarts Open5GS EPC services on `pc808`.
- Pre-creates network namespaces `ue1`..`ue50` on `pc801` with isolated veth routing (`10.200.{n}.0/24`) and NAT rules.
- Launches the 50-UE accumulation sweep in the background.

#### How to Monitor Live Progress
**Where to run:** On the **Control Host**
```bash
tail -f /tmp/run_oai_50ue.log
```

---

### Step 4: Post-Collection Metrics & MCS Backfill
**Where to run:** On the **Control Host** (repository root)
```bash
python3 scripts/working_oai/backfill_oai_mcs.py
```
**What this does:**
- Queries `pc801` execution logs in batch mode.
- Extracts scheduled DL/UL MCS, CQI, SINR, and PHR metrics.
- Populates all 177 columns in `results/ver_eval/oai/ue_results_50.csv` and mirrors to `~/Desktop/oai_ue_results_50.csv`.

---

### Step 5 (Optional): Single-UE Targeted Retest
**Where to run:** On the **Control Host** (repository root)
```bash
python3 scripts/working_oai/rerun_failed_oai_ues.py
```
Use this if an individual UE encountered transient contention during initial attach.

---

## Dynamic Load Balancing (BPEA-LB)

**Where to run:** On the **Control Host** (repository root)
```bash
python3 scripts/working_oai/bpea_load_balancing.py
```
Evaluates the **Bounded-Performance Energy-Aware Dynamic Load Balancing (BPEA-LB)** algorithm across gNodeBs using dynamic entropy-based feature weights and critical bulk migration thresholds.

---

## Direct Node SSH Reference (For Manual Inspection)

If you need to log directly into any of the POWDER nodes to inspect logs or interfaces:

- **Core Node**:
  ```bash
  ssh -i ~/.ssh/id_ed25519 saish@pc808.emulab.net
  ```
- **eNodeB Node**:
  ```bash
  ssh -i ~/.ssh/id_ed25519 saish@pc802.emulab.net
  ```
- **UE Host Node**:
  ```bash
  ssh -i ~/.ssh/id_ed25519 saish@pc801.emulab.net
  ```

---

## Dataset Artifacts

All results are automatically flushed and synced to:
- **Repository CSV**: [`results/ver_eval/oai/ue_results_50.csv`](../../results/ver_eval/oai/ue_results_50.csv)
- **Local Desktop Mirror**: `~/Desktop/oai_ue_results_50.csv`

### Metrics Captured (177 Columns):
- **Attach Profile**: `attach_ms`, `attach_ok`, `ue_ip`.
- **Latency & Loss**: `ping_avg_ms`, `ping_min_ms`, `ping_max_ms`, `ping_jitter_ms`, `ping_loss_pct`.
- **Downlink TCP Throughput**: 18 standard rates (`dl_1m_mbps` to `dl_50m_mbps`) with loss percentages.
- **Uplink TCP Throughput**: 18 standard rates (`ul_1m_mbps` to `ul_50m_mbps`) with loss percentages.
- **RAPL Energy Profiling**: `gnb_rapl_pkg0_w`, `gnb_rapl_pkg1_w`, `gnb_rapl_total_w`.
- **Turbostat CPU Profiling**: 18 hardware counter columns (`gnb_ts_avg_mhz`, `gnb_ts_busy_pct`, `gnb_ts_ipc`, `gnb_ts_irq`, etc.).
- **PHY / RAN Metrics**: `ran_dl_mcs`, `ran_ul_mcs`, `phy_dl_mcs_avg`, `phy_dl_snr_avg`, `ran_cqi`, `ran_phr`, `ran_pusch_sinr`.
