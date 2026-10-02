# srsRAN v22.10 50-UE Evaluation & Benchmarking Suite

This directory contains the complete end-to-end automation suite for deploying, configuring, compiling, executing, and collecting datasets for **srsRAN v22.10 (4G LTE)** on POWDER testbed nodes (`pc808` Core/EPC, `pc802` eNB host, `pc801` UE host), writing to the complete 177-column evaluation schema.

---

## Architecture & Topology

| Node Name | Node Hostname / IP | Role / Component |
|:---|:---|:---|
| **Control Host** | Local Workstation / Laptop | Orchestrates scripts via SSH key `~/.ssh/id_ed25519` |
| **Core Node** | `pc808.emulab.net` (`10.10.1.1`) | Open5GS EPC (MME, SGW-C, SGW-U, SMF, UPF) + iperf3 server |
| **eNB Host** | `pc802.emulab.net` (`10.10.1.2`) | `srsenb` v22.10 (Band 7, 50 PRB, ZMQ radio backend) |
| **UE Host** | `pc801.emulab.net` (`10.10.1.4`) | `srsue` v22.10 (50 UEs in network namespaces `ue1`..`ue50`) |

---

## Step-by-Step Execution Guide

> **Note on Execution Location:**
> All commands below are designed to be run from the **root directory of the `openran-version` repository on your local Control Host** (`/Users/.../playground/` or repository root).
> The orchestration scripts connect over SSH to `pc808`, `pc802`, and `pc801` automatically.

---

### Step 1: Install Dependencies & Build srsRAN v22.10
**Where to run:** On the **Control Host** (repository root)
```bash
bash scripts/working_v22/install_and_build_v22.sh
```
**What this does automatically:**
1. Connects to `pc802` (eNB) and `pc801` (UE) over SSH.
2. Installs dependencies (`libboost-all-dev`, `libzmq3-dev`, `libfftw3-dev`, `libmbedtls-dev`, `libsctp-dev`, `libconfig++-dev`, `cmake`, `ninja-build`).
3. Clones `srsRAN_4G` (tag `release_22_10`) into `/opt/srsRAN_v22`.
4. Compiles with ZeroMQ RF driver enabled (`-DENABLE_ZMQ=ON`).

---

### Step 2: Generate & Deploy 50 eNB and 50 UE Configurations
**Where to run:** On the **Control Host** (repository root)
```bash
python3 scripts/working_v22/gen_configs_v22.py
```
**Generated artifacts:**
- **eNB configs on `pc802`**: `/etc/srsenb_v22/enb_ue1.conf` ... `/etc/srsenb_v22/enb_ue50.conf`
  - ZMQ TX ports: `40010`, `40020`, ... `40500`.
  - GTP bind IPs: `10.10.2.1` ... `10.10.2.50`.
- **UE configs on `pc801`**: `/etc/srsue_v22/ue1.conf` ... `/etc/srsue_v22/ue50.conf`
  - Assigned namespaces `ue1`..`ue50` and matching IMSIs `999700000000001` .. `999700000000050`.

---

### Step 3: Launch Clean-Slate 50-UE Experiment
**Where to run:** On the **Control Host** (repository root)
```bash
python3 scripts/working_v22/cleanslate_and_launch_v22.py
```
**What this does automatically:**
- Terminates stale `srsenb`, `srsue`, and `iperf3` processes on all nodes.
- Restarts Open5GS EPC services on `pc808`.
- Cleans and prepares network namespaces and TUN devices on `pc801`.
- Launches `scripts/working_v22/run_v22_50ue.py` in the background.

#### How to Monitor Live Progress
**Where to run:** On the **Control Host**
```bash
tail -f /tmp/run_v22_50ue.log
```

---

### Direct Node SSH Reference (For Manual Inspection)
- **Core Node**: `ssh -i ~/.ssh/id_ed25519 saish@pc808.emulab.net`
- **eNB Node**: `ssh -i ~/.ssh/id_ed25519 saish@pc802.emulab.net`
- **UE Node**: `ssh -i ~/.ssh/id_ed25519 saish@pc801.emulab.net`

---

## Dataset Artifacts

All results are automatically flushed and synced to:
- **Repository CSV**: [`results/ver_eval/v22_10/ue_results_50.csv`](../../results/ver_eval/v22_10/ue_results_50.csv)
- **Local Desktop Mirror**: `~/Desktop/v22_10_ue_results_50.csv`
