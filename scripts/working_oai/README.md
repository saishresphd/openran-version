# OpenAirInterface (OAI) 4G LTE 50-UE Evaluation Suite

This directory contains the complete reproduction and automation suite for deploying, configuring, and benchmarking OpenAirInterface (OAI) 4G LTE against srsRAN versions across the POWDER testbed nodes (`pc808` Core, `pc802` eNodeB, `pc801` UE host).

---

## 1. Prerequisites and Installation

### Architecture Mapping
- **Core / EPC (`pc808` - 10.10.1.1)**: Open5GS EPC (MME, SGW-C, SGW-U, SMF, UPF).
- **eNodeB Host (`pc802` - 10.10.1.2)**: OAI 4G `lte-softmodem` (Band 7, 50 PRB, RFsimulator).
- **UE Host (`pc801` - 10.10.1.4)**: OAI 4G `lte-uesoftmodem` (50 UEs in individual netns `ue1`..`ue50`).

### Step 1: Install Dependencies and Build Binaries
Execute the installation and compilation script:
```bash
bash scripts/working_oai/install_and_build_oai.sh
```

This installs system packages (`git`, `cmake`, `ninja-build`, `libboost-all-dev`, `libzmq3-dev`, `libatlas-base-dev`, `libblas-dev`, `liblapack-dev`, `libconfig-dev`, `libsctp-dev`, `ccache`), clones OpenAirInterface 5G at `/opt/openairinterface5g`, installs external dependencies via `build_oai -I`, and compiles:
- `lte-softmodem` with RFsimulator and Telnet server enabled on `pc802`.
- `lte-uesoftmodem` with RFsimulator on `pc801`.

---

## 2. Configuration Generation

Generate and deploy the 50 eNodeB and 50 UE configuration files:
```bash
python3 scripts/working_oai/gen_configs_oai.py
```
- **eNodeB configurations**: `/etc/oai_enb/enb_ue1.conf` ... `/etc/oai_enb/enb_ue50.conf`
  - RFsimulator listening on ports `40010`, `40020`, ... `40500`.
  - GTP bind IPs: `10.10.2.1` ... `10.10.2.50`.
- **UE configurations**: `/etc/oai_ue/ue1.conf` ... `/etc/oai_ue/ue50.conf`
  - Matching IMSIs `999700000000001` .. `999700000000050`.

---

## 3. Clean-Slate Launch and Experimentation

Execute the clean-slate launcher to teardown previous processes, restart EPC services, initialize network namespaces, and launch the 50-UE accumulation benchmark:
```bash
python3 scripts/working_oai/cleanslate_and_launch_oai.py
```

### Experiment Metrics Output
The benchmark outputs 170+ metrics per UE row:
- **Repository Dataset**: `results/ver_eval/oai/ue_results_50.csv`
- **Desktop Mirror**: `~/Desktop/oai_ue_results_50.csv`
- **Execution Log**: `/tmp/run_oai_50ue.log`

Metrics collected include:
- Attach timing and status (`attach_ms`, `attach_ok`).
- ICMP round-trip latency, jitter, and packet loss.
- 18 TCP Downlink throughput rates (1M to 50M).
- 18 TCP Uplink throughput rates (1M to 50M).
- Dual-socket CPU RAPL power (`gnb_rapl_pkg0_w`, `gnb_rapl_pkg1_w`, `gnb_rapl_total_w`) and Turbostat hardware counters.
- RAN and PHY link metrics (MCS, BLER, CQI, SINR, RSRP).
