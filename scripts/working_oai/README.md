# OpenAirInterface (OAI) 4G LTE 50-UE Evaluation & Benchmarking Suite

This directory contains the complete end-to-end automation suite for deploying, configuring, compiling, executing, and collecting datasets for **OpenAirInterface (OAI) 4G LTE** on the POWDER testbed nodes (`pc808` Core/EPC, `pc802` eNB host, `pc801` UE host), maintaining 100% schema parity (177 metrics) with srsRAN v22.10, v23.11, and v25.10 benchmarks.

---

## Architecture & Topology

- **Core / EPC (`pc808` - `10.10.1.1`)**: Open5GS EPC (MME, SGW-C, SGW-U, SMF, UPF).
- **eNodeB Host (`pc802` - `10.10.1.2`)**: OAI 4G `lte-softmodem` (Band 7, 50 PRB / 10 MHz, RFsimulator TCP server).
- **UE Host (`pc801` - `10.10.1.4`)**: OAI 4G `lte-uesoftmodem` (50 UEs in network namespaces `ue1`..`ue50` connected via RFsimulator client sockets).

---

## Step-by-Step Execution Guide

### Step 1: Install Dependencies, Apply Patches & Build OAI
Run the automated installation and compilation script from your control machine:
```bash
bash scripts/working_oai/install_and_build_oai.sh
```
**What this does automatically:**
1. Installs all required OS and build toolchains (`cmake`, `ninja-build`, `libboost-all-dev`, `libzmq3-dev`, `libsctp-dev`, `ccache`, `libconfig-dev`, `libatlas-base-dev`) on both `pc802` and `pc801`.
2. Clones OpenAirInterface at `/opt/openairinterface5g` and checks out the stable build tag.
3. **Applies eNB MBMS Patch**: Disables unconditional `ENB_NAS_USE_TUN_W_MBMS_BIT` in `executables/lte-softmodem.c` to avoid device contention on `oaitun_enm1` during multi-instance execution.
4. **Applies UE NAS TAI Patch**: Updates `openair3/NAS/UE/EMM/SAP/emm_recv.c` to accept Open5GS non-consecutive TAC Attach Accept lists (`cause=99` fix).
5. Compiles `lte-softmodem` (on `pc802`) and `lte-uesoftmodem` (on `pc801`) with RFsimulator support.

---

### Step 2: Generate & Deploy 50 eNB and 50 UE Configurations
Generate and install the configuration files and USIM NVRAM records:
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
Run the clean-slate launcher to teardown any existing processes, reset EPC, prepare network namespaces with routing (`10.200.{n}.0/24`), and execute the 50-UE accumulation sweep:
```bash
python3 scripts/working_oai/cleanslate_and_launch_oai.py
```

#### Monitor Live Progress
```bash
tail -f /tmp/run_oai_50ue.log
```

---

### Step 4: Post-Collection Metrics & MCS Backfill
Once the sweep completes (or as UEs finish), execute the batch backfill script to enrich the dataset with full PHY/MAC MCS and RAN parameters extracted from the host execution logs:
```bash
python3 scripts/working_oai/backfill_oai_mcs.py
```

---

### Step 5 (Optional): Single-UE Targeted Retest
If any UE experiences random wireless contention during attach, retest without re-running the entire sweep:
```bash
python3 scripts/working_oai/rerun_failed_oai_ues.py
```

---

## Dynamic Load Balancing (BPEA-LB)

To evaluate and test the **Bounded-Performance Energy-Aware Dynamic Load Balancing (BPEA-LB)** algorithm across gNodeBs:
```bash
python3 scripts/working_oai/bpea_load_balancing.py
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
