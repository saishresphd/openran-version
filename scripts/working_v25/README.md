# srsRAN v25.10 50-UE Evaluation & Benchmarking Suite

This directory contains the complete end-to-end automation suite for deploying, configuring, compiling, executing, and collecting datasets for **srsRAN v25.10 (Project / 5G SA and LTE)** on POWDER testbed nodes (`pc808` Core/EPC, `pc802` eNB host, `pc801` UE host), writing to the complete 177-column evaluation schema.

---

## Architecture & Topology

| Node Name | Node Hostname / IP | Role / Component |
|:---|:---|:---|
| **Control Host** | Local Workstation / Laptop | Orchestrates scripts via SSH key `~/.ssh/id_ed25519` |
| **Core Node** | `pc808.emulab.net` (`10.10.1.1`) | Open5GS EPC / 5GC (MME/AMF, SGW/UPF) + iperf3 server |
| **eNB Host** | `pc802.emulab.net` (`10.10.1.2`) | `srsenb` v25.10 (Band 7, 50 PRB, ZMQ radio backend) |
| **UE Host** | `pc801.emulab.net` (`10.10.1.4`) | `srsue` v25.10 (50 UEs in network namespaces `ue1`..`ue50`) |

---

## Step-by-Step Execution Guide

> **Note on Execution Location:**
> All commands below are designed to be run from the **root directory of the `openran-version` repository on your local Control Host** (`/Users/.../playground/` or repository root).
> The orchestration scripts connect over SSH to `pc808`, `pc802`, and `pc801` automatically.

---

### Step 1: Install Dependencies & Build srsRAN v25.10
**Where to run:** On the **Control Host** (repository root)
```bash
bash scripts/working_v25/install_and_build_v25.sh
```
**What this does automatically:**
1. Connects to `pc802` (eNB) and `pc801` (UE) over SSH.
2. Installs modern build toolchains and dependencies (`libboost-all-dev`, `libzmq3-dev`, `libfftw3-dev`, `libmbedtls-dev`, `libsctp-dev`, `libconfig++-dev`, `cmake`, `ninja-build`).
3. Clones srsRAN (tag `release_25_10` / Project) into `/opt/srsRAN_v25`.
4. Compiles `srsenb` and `srsue` with ZeroMQ RF driver support.

---

### Step 2: Generate & Deploy 50 eNB and 50 UE Configurations
**Where to run:** On the **Control Host** (repository root)
```bash
python3 scripts/working_v25/gen_configs_v25.py
```
**Generated artifacts:**
- **eNB configs on `pc802`**: `/etc/srsenb_v25/enb_ue1.conf` ... `/etc/srsenb_v25/enb_ue50.conf`
  - ZMQ TX ports: `40010`, `40020`, ... `40500`.
  - GTP bind IPs: `10.10.2.1` ... `10.10.2.50`.
- **UE configs on `pc801`**: `/etc/srsue_v25/ue1.conf` ... `/etc/srsue_v25/ue50.conf`
  - Assigned namespaces `ue1`..`ue50` and matching IMSIs `999700000000001` .. `999700000000050`.

---

### Step 3: Launch Clean-Slate 50-UE Experiment
**Where to run:** On the **Control Host** (repository root)
```bash
python3 scripts/working_v25/cleanslate_and_launch_v25.py
```
**What this does automatically:**
- Terminates stale processes on all nodes.
- Restarts Open5GS core services on `pc808`.
- Cleans and prepares network namespaces and TUN devices on `pc801`.
- Launches `scripts/working_v25/run_v25_50ue.py` in the background.

#### How to Monitor Live Progress
**Where to run:** On the **Control Host**
```bash
tail -f /tmp/run_v25_50ue.log
```

---

### Step 4: Dynamic Load Balancing Evaluation (BPEA-LB)
**Where to run:** On the **Control Host** (repository root)
```bash
python3 scripts/working_oai/bpea_load_balancing.py
```

---

### Direct Node SSH Reference (For Manual Inspection)
- **Core Node**: `ssh -i ~/.ssh/id_ed25519 saish@pc808.emulab.net`
- **eNB Node**: `ssh -i ~/.ssh/id_ed25519 saish@pc802.emulab.net`
- **UE Node**: `ssh -i ~/.ssh/id_ed25519 saish@pc801.emulab.net`

---

## Dataset Artifacts

All results are automatically flushed and synced to:
- **Repository CSV**: [`results/ver_eval/v25_10/ue_results_50.csv`](../../results/ver_eval/v25_10/ue_results_50.csv)
- **Local Desktop Mirror**: `~/Desktop/v25_10_ue_results_50.csv`
