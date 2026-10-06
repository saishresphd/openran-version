# POWDER Open RAN — 5G SA Experiment

End-to-end **5G Standalone (SA)** experiment on the POWDER Wireless Testbed comparing two open-source 5G stacks.

| | Experiment A | Experiment B |
|---|---|---|
| **gNB** | srsRAN Project **v25.10.0** | OAI nr-softmodem **2026.w40** |
| **UE** | srsRAN 4G **v23.04** (NR mode) | OAI nr-uesoftmodem **2026.w40** |
| **Core** | Open5GS **2.8.0** (5GC) | Open5GS **2.8.0** (5GC) |
| **Virtual RF** | ZMQ (TCP) | RFsimulator (TCP) |

> ⬆️ **Upgrade from [4G LTE experiment](../README.md)**  
> Previous: `srsenb` + Open5GS EPC (S1AP/MME, EARFCN 3350, MCC=999)  
> This: true 5G NR SA — `gnb`/`nr-softmodem` + Open5GS 5GC (NGAP/AMF, NR ARFCN, MCC=001)

---

## Software Versions

| Component | Version | Source | Node |
|---|---|---|---|
| **Open5GS** | 2.8.0 | `ppa:open5gs/latest` | core (pc808) |
| **MongoDB** | 7.0.43 | `mongodb.org/apt/ubuntu` | core (pc808) |
| **srsRAN Project** (gnb) | 25.10.0 | github.com/srsran/srsRAN_Project @ `4bf1543` | gnb1 (pc802) |
| **srsRAN 4G** (srsUE NR) | 23.04 | github.com/srsran/srsRAN_4G @ `release_23_04` | uehost1 (pc801) |
| **libzmq** | built from source | github.com/zeromq/libzmq `main` | pc802, pc801 |
| **OAI nr-softmodem** | 2026.w40 | gitlab.eurecom.fr/oai/openairinterface5g @ `f8f7695` | gnb2 (pc811) |
| **OAI nr-uesoftmodem** | 2026.w40 | gitlab.eurecom.fr/oai/openairinterface5g @ `f8f7695` | uehost2 (pc818) |
| **Ubuntu** | 22.04.2 LTS | emulab-ops/UBUNTU22-64-STD | all nodes |
| **Kernel** | 5.15.0-187-generic | — | all nodes |

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Node Allocation](#node-allocation)
3. [Key Differences vs 4G Experiment](#key-differences-vs-4g-experiment)
4. [Step 1 — Core: Install Open5GS 5GC](#step-1--core-install-open5gs-5gc)
5. [Step 2 — gnb1: Build srsRAN Project gNB](#step-2--gnb1-build-srsran-project-gnb-v25100)
6. [Step 3 — uehost1: Build srsUE NR](#step-3--uehost1-build-srsue-nr-v2304)
7. [Step 4 — gnb2: Build OAI gNB](#step-4--gnb2-build-oai-gnb-2026w40)
8. [Step 5 — uehost2: Build OAI nrUE](#step-5--uehost2-build-oai-nrue-2026w40)
9. [Step 6 — Add 5G Subscribers](#step-6--add-5g-subscribers)
10. [Step 7 — Start Experiment A](#step-7--start-experiment-a-srsran)
11. [Step 8 — Start Experiment B](#step-8--start-experiment-b-oai)
12. [Step 9 — Ping & iPerf3 Tests](#step-9--ping--iperf3-tests)
13. [Stopping the Network](#stopping-the-network)
14. [Troubleshooting](#troubleshooting)
15. [Key Parameters Reference](#key-parameters-reference)

---

## Architecture Overview

```
┌──────────────────────────── POWDER LAN 10.10.1.0/24 ──────────────────────────────┐
│                                                                                    │
│  ┌──────────────┐  N2 (NGAP/SCTP:38412)  ┌──────────────┐  ZMQ TCP  ┌──────────┐ │
│  │    core      │◄──────────────────────►│    gnb1      │◄─────────►│ uehost1  │ │
│  │   pc808      │  N3 (GTP-U/UDP:2152)   │    pc802     │ :2000/2001│  pc801   │ │
│  │  10.10.1.1   │◄──────────────────────►│  10.10.1.2   │           │10.10.1.4 │ │
│  │              │                        │ srsRAN v25.10│           │ srsUE    │ │
│  │ Open5GS 5GC  │                        └──────────────┘           │ v23.04   │ │
│  │ v2.8.0       │                                                    └──────────┘ │
│  │              │  N2 (NGAP/SCTP:38412)  ┌──────────────┐  RFsim   ┌──────────┐  │
│  │ AMF UPF SMF  │◄──────────────────────►│    gnb2      │◄─────────►│ uehost2  │ │
│  │ NRF AUSF UDM │  N3 (GTP-U/UDP:2152)  │    pc811     │ TCP:4043  │  pc818   │ │
│  │              │◄──────────────────────►│  10.10.1.3   │           │10.10.1.5 │ │
│  │ MongoDB 7.0  │                        │ OAI 2026.w40 │           │ OAI nrUE │ │
│  │ ogstun       │                        └──────────────┘           │ 2026.w40 │ │
│  │ 10.45.0.1/16 │                                                    └──────────┘ │
│  └──────────────┘                                                                 │
└────────────────────────────────────────────────────────────────────────────────────┘
```

### Node Allocation

| POWDER ID | Host | LAN IP | Role | Key Software |
|---|---|---|---|---|
| core | pc808.emulab.net | 10.10.1.1 | Open5GS 5GC | Open5GS 2.8.0, MongoDB 7.0.43 |
| gnb1 | pc802.emulab.net | 10.10.1.2 | srsRAN gNB | srsRAN Project 25.10.0, libzmq |
| gnb2 | pc811.emulab.net | 10.10.1.3 | OAI gNB | OAI 2026.w40 (nr-softmodem) |
| uehost1 | pc801.emulab.net | 10.10.1.4 | srsUE NR | srsRAN 4G 23.04 (srsUE) |
| uehost2 | pc818.emulab.net | 10.10.1.5 | OAI nrUE | OAI 2026.w40 (nr-uesoftmodem) |

---

## Key Differences vs 4G Experiment

| Aspect | Previous 4G LTE | This 5G SA |
|---|---|---|
| **Core interface** | S1AP port 36412 — MME | NGAP/N2 port 38412 — AMF |
| **Core components** | MME, SGW-C/U, HSS | AMF, UPF, SMF, NRF, AUSF, UDM |
| **Core version** | Open5GS 2.8.0 (EPC mode) | Open5GS 2.8.0 (5GC mode) |
| **srsRAN gNB binary** | `srsenb` (LTE eNodeB) | `gnb` (srsRAN Project 25.10.0) |
| **srsRAN UE binary** | `srsue` (LTE, release=8) | `srsue` (NR, release=15) |
| **OAI gNB binary** | `lte-softmodem` | `nr-softmodem` (2026.w40) |
| **OAI UE binary** | `lte-uesoftmodem` | `nr-uesoftmodem` (2026.w40) |
| **Radio standard** | LTE, EARFCN 3350, Band 7 | NR, ARFCN 368500 Band n3 / 641272 Band n78 |
| **PLMN** | MCC=999, MNC=70, TAC=1 | MCC=001, MNC=01, TAC=7 |
| **Virtual RF (srsRAN)** | ZMQ | ZMQ |
| **Virtual RF (OAI)** | RFsimulator | RFsimulator |
| **No. of UEs** | 20 (10 per gNB) | 1 per experiment |

---

## Step 1 — Core: Install Open5GS 5GC

**Version: Open5GS 2.8.0 + MongoDB 7.0.43**

```bash
ssh saish@pc808.emulab.net

# 1.1 Install Open5GS 2.8.0 from PPA
sudo add-apt-repository ppa:open5gs/latest -y
sudo apt update && sudo apt install -y open5gs
dpkg -l open5gs | grep ii   # Verify: 2.8.0~jammy5

# 1.2 Install MongoDB 7.0.43
curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc -o /tmp/mongo.asc
sudo gpg --batch --yes --dearmor \
  -o /usr/share/keyrings/mongodb-server-7.0.gpg /tmp/mongo.asc
echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] \
  https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/7.0 multiverse" \
  | sudo tee /etc/apt/sources.list.d/mongodb-org-7.0.list
sudo apt update && sudo apt install -y mongodb-org
sudo systemctl enable --now mongod
mongosh --eval 'db.runCommand({ping:1})' --quiet   # Expected: { ok: 1 }

# 1.3 Deploy configs
sudo cp 5g-experiment/core/amf.yaml /etc/open5gs/amf.yaml
sudo cp 5g-experiment/core/upf.yaml /etc/open5gs/upf.yaml

# 1.4 IP forwarding + NAT
sudo sysctl -w net.ipv4.ip_forward=1
echo "net.ipv4.ip_forward=1" | sudo tee -a /etc/sysctl.conf
sudo iptables -t nat -A POSTROUTING -s 10.45.0.0/16 ! -o ogstun -j MASQUERADE
sudo iptables -A FORWARD -i ogstun -j ACCEPT
sudo apt install -y iptables-persistent && sudo netfilter-persistent save

# 1.5 Stop 4G EPC services, start 5GC
for svc in mmed sgwcd sgwud; do
  sudo systemctl stop open5gs-${svc} 2>/dev/null || true
  sudo systemctl disable open5gs-${svc} 2>/dev/null || true
done
sudo systemctl restart open5gs-nrfd
sleep 2
for svc in scpd amfd smfd upfd ausfd udmd pcfd nssfd bsfd udrd; do
  sudo systemctl restart open5gs-${svc}
done

# 1.6 Verify AMF NGAP port (SCTP, not TCP)
sudo ss -lnp --sctp | grep 38412
# Expected: LISTEN  10.10.1.1:38412  (open5gs-amfd)
```

> **Known issue (v2.8.0):** AMF crashes with `No amf.time.t3512.value` if the
> `time:` block is missing from `amf.yaml`. The config in this repo includes it.

---

## Step 2 — gnb1: Build srsRAN Project gNB (v25.10.0)

```bash
ssh saish@pc802.emulab.net

# 2.1 Build dependencies
sudo apt update && sudo apt install -y \
  cmake make gcc g++ pkg-config git \
  libfftw3-dev libmbedtls-dev libsctp-dev \
  libyaml-cpp-dev libgtest-dev libzmq3-dev

# 2.2 Build libzmq from source (ensures ZMQ version compatibility)
cd /tmp && git clone --depth=1 https://github.com/zeromq/libzmq.git
cd libzmq && mkdir build && cd build
cmake .. -DENABLE_DRAFTS=OFF -DCMAKE_BUILD_TYPE=Release
make -j$(nproc) && sudo make install && sudo ldconfig

# 2.3 Clone srsRAN Project — fetch main branch (default branch is 'archive')
cd /tmp && git clone https://github.com/srsran/srsRAN_Project.git
cd srsRAN_Project
git fetch origin main:main --depth=1 && git checkout main
# Confirm version 25.10.0:
grep "Building srsRAN version" /tmp/gnb_build.log 2>/dev/null || true
# Commit: 4bf1543

# 2.4 Configure + build
mkdir -p /tmp/srsRAN_Project/build
cmake /tmp/srsRAN_Project -B /tmp/srsRAN_Project/build \
  -DCMAKE_BUILD_TYPE=Release \
  -DENABLE_EXPORT=ON \
  -DENABLE_ZEROMQ=ON
# Verify: "Found libZEROMQ: /usr/local/include, /usr/local/lib/libzmq.so"

bash -c "make -C /tmp/srsRAN_Project/build -j\$(nproc) gnb"
# ~15 min on d430 — binary: ~50 MB
sudo install /tmp/srsRAN_Project/build/apps/gnb/gnb /usr/local/bin/gnb

# 2.5 Deploy config
sudo mkdir -p /etc/srsran
sudo cp 5g-experiment/gnb1/gnb_zmq.yaml /etc/srsran/gnb_zmq.yaml
```

---

## Step 3 — uehost1: Build srsUE NR (v23.04)

```bash
ssh saish@pc801.emulab.net

# 3.1 Dependencies + libzmq
sudo apt update && sudo apt install -y \
  cmake make gcc g++ pkg-config git \
  libfftw3-dev libmbedtls-dev libsctp-dev \
  libconfig++-dev libboost-program-options-dev libzmq3-dev

# Build libzmq (same as gnb1)
cd /tmp && git clone --depth=1 https://github.com/zeromq/libzmq.git
cd libzmq/build && cmake .. -DENABLE_DRAFTS=OFF && make -j$(nproc)
sudo make install && sudo ldconfig

# 3.2 Clone srsRAN 4G at release_23_04
cd /tmp
git clone --depth=1 https://github.com/srsran/srsRAN_4G.git \
  --branch release_23_04
cd srsRAN_4G && mkdir build && cd build

cmake .. \
  -DCMAKE_BUILD_TYPE=Release \
  -DENABLE_WERROR=OFF \
  -DENABLE_ZMQ=ON \
  -DENABLE_UHD=OFF
# Verify: "Found libZEROMQ: /usr/local/lib/libzmq.so" and "ENABLE_ZMQ"

# Fix ccache permission if previous root build exists:
# sudo chown -R saish: ~/.cache/ccache 2>/dev/null; rm -rf ~/.cache/ccache

CCACHE_DISABLE=1 make -j$(nproc) srsue
# Binary: /tmp/srsRAN_4G/build/srsue/src/srsue (~28 MB)
sudo install /tmp/srsRAN_4G/build/srsue/src/srsue /usr/local/bin/srsue

# 3.3 Deploy config + create network namespace
sudo mkdir -p /etc/srsran
sudo cp 5g-experiment/uehost1/ue_zmq.conf /etc/srsran/ue_zmq.conf
sudo ip netns add ue1 2>/dev/null || true
```

---

## Step 4 — gnb2: Build OAI gNB (2026.w40)

```bash
ssh saish@pc811.emulab.net

# 4.1 Dependencies
sudo apt update && sudo apt install -y \
  cmake make gcc g++ git pkg-config \
  libfftw3-dev libmbedtls-dev libsctp-dev \
  libconfig++-dev libboost-all-dev \
  libblas-dev liblapack-dev libatlas-base-dev \
  python3-dev iperf3

# 4.2 Clone OAI at tag 2026.w40
cd /tmp
git clone --depth=1 https://gitlab.eurecom.fr/oai/openairinterface5g.git \
  --branch 2026.w40
# Commit: f8f7695
cd openairinterface5g/cmake_targets

# 4.3 Install system build deps
sudo ./build_oai -I --nrUE 2>&1 | tail -5

# 4.4 Build nr-softmodem with --noavx512 (required on d430 / older CPUs)
./build_oai --gNB --noavx512 2>&1 | tail -10
# Binary: ran_build/build/nr-softmodem (~125 MB)
ls -lh ran_build/build/nr-softmodem

# 4.5 Deploy config
sudo mkdir -p /etc/oai_gnb
sudo cp 5g-experiment/gnb2/gnb.sa.rfsim.conf /etc/oai_gnb/gnb.sa.rfsim.conf
# Verify interface name matches your node (default: enp4s0f1 on pc811 d430)
ip -o -4 addr show | grep 10.10.1.3 | awk '{print $2}'   # confirms interface
```

> **Known issue:** If a previous build was run as root, `cmake_targets/ran_build`
> will be root-owned. Fix: `sudo rm -rf ran_build` before rebuilding.  
> Use `--noavx512` flag — AVX-512 is not available on d430 nodes.

---

## Step 5 — uehost2: Build OAI nrUE (2026.w40)

```bash
ssh saish@pc818.emulab.net

# 5.1 Same deps as gnb2
sudo apt update && sudo apt install -y \
  cmake make gcc g++ git pkg-config \
  libfftw3-dev libmbedtls-dev libsctp-dev \
  libconfig++-dev libboost-all-dev \
  libblas-dev liblapack-dev libatlas-base-dev \
  python3-dev iperf3

# 5.2 Clone OAI at tag 2026.w40
cd /tmp
git clone --depth=1 https://gitlab.eurecom.fr/oai/openairinterface5g.git \
  --branch 2026.w40
cd openairinterface5g/cmake_targets
sudo ./build_oai -I --nrUE 2>&1 | tail -5

# 5.3 Build nr-uesoftmodem
./build_oai --nrUE --noavx512 2>&1 | tail -10
# Binary: ran_build/build/nr-uesoftmodem (~47 MB)
ls -lh ran_build/build/nr-uesoftmodem

# 5.4 Deploy config
sudo mkdir -p /etc/oai_ue
sudo cp 5g-experiment/uehost2/ue.conf /etc/oai_ue/ue.conf
```

---

## Step 6 — Add 5G Subscribers

On **core (pc808)** — both subscribers use MCC=001, MNC=01, SST=1:

```bash
mongosh open5gs << 'MONGOEOF'
// Remove any old 001-PLMN test entries
db.subscribers.deleteMany({ imsi: { $regex: /^001/ } });

// --- Subscriber A: srsUE NR v23.04 (Experiment A) ---
db.subscribers.insertOne({
  schema_version: 1,
  imsi: "001010123456780",
  msisdn: [], imeisv: [],
  security: {
    k:   "00112233445566778899aabbccddeeff",
    amf: "8000", op: null,
    opc: "63bfa50ee6523365ff14c1f45f88737d"
  },
  ambr: { downlink: { value: 1, unit: 3 }, uplink: { value: 1, unit: 3 } },
  slice: [{ sst: 1, default_indicator: true,
    session: [{ name: "srsapn", type: 3,
      qos: { index: 9, arp: { priority_level: 8,
             pre_emption_capability: 1, pre_emption_vulnerability: 1 } },
      ambr: { downlink: { value: 1, unit: 3 }, uplink: { value: 1, unit: 3 } },
      ue: { addr: "0.0.0.0" }, pcc_rule: [] }] }],
  access_restriction_data: 32, subscriber_status: 0,
  network_access_mode: 0, subscribed_rau_tau_timer: 12
});

// --- Subscriber B: OAI nrUE 2026.w40 (Experiment B) ---
db.subscribers.insertOne({
  schema_version: 1,
  imsi: "001010000000001",
  msisdn: [], imeisv: [],
  security: {
    k:   "fec86ba6eb707ed08905757b1bb44b8f",
    amf: "8000", op: null,
    opc: "C42449363BBAD02B66D16BC975D77CC1"
  },
  ambr: { downlink: { value: 1, unit: 3 }, uplink: { value: 1, unit: 3 } },
  slice: [{ sst: 1, default_indicator: true,
    session: [{ name: "oai", type: 3,
      qos: { index: 9, arp: { priority_level: 8,
             pre_emption_capability: 1, pre_emption_vulnerability: 1 } },
      ambr: { downlink: { value: 1, unit: 3 }, uplink: { value: 1, unit: 3 } },
      ue: { addr: "0.0.0.0" }, pcc_rule: [] }] }],
  access_restriction_data: 32, subscriber_status: 0,
  network_access_mode: 0, subscribed_rau_tau_timer: 12
});

print("5G Subscribers added: " + db.subscribers.countDocuments({ imsi: { $regex: /^001/ } }));
MONGOEOF
# Expected: 5G Subscribers added: 2
```

---

## Step 7 — Start Experiment A (srsRAN)

**Stack: srsRAN Project 25.10.0 gNB + srsRAN 4G 23.04 srsUE + Open5GS 5GC 2.8.0**

**Critical order: Core already running → start gNB first → then UE**

```bash
# --- Terminal 1: Verify core is ready ---
ssh saish@pc808.emulab.net 'sudo ss -lnp --sctp | grep 38412'
# Must show: LISTEN  10.10.1.1:38412

# --- Terminal 2: Start srsRAN Project gNB (gnb1) ---
ssh saish@pc802.emulab.net
sudo pkill -9 gnb 2>/dev/null; sleep 1
sudo gnb -c /etc/srsran/gnb_zmq.yaml
# Success indicators:
#   Cell pci=1, bw=10 MHz, dl_arfcn=368500 (n3), dl_freq=1842.5 MHz
#   AMF connected: gNB-N2[10.10.1.2] connected

# --- Terminal 3: Start srsUE NR (uehost1) ---
ssh saish@pc801.emulab.net
sudo ip netns exec ue1 ip link del tun_srsue 2>/dev/null || true
sudo srsue /etc/srsran/ue_zmq.conf
# Success indicators:
#   Random Access Complete. c-rnti=0x4601, ta=0
#   RRC Connected
#   PDU Session Establishment successful. IP: 10.45.x.x
#   RRC NR reconfiguration successful.
```

---

## Step 8 — Start Experiment B (OAI)

**Stack: OAI nr-softmodem 2026.w40 + OAI nr-uesoftmodem 2026.w40 + Open5GS 5GC 2.8.0**

Stop Experiment A first, then:

```bash
# --- Terminal 2: Start OAI gNB (gnb2) — RFsimulator server mode ---
ssh saish@pc811.emulab.net
sudo pkill -9 nr-softmodem 2>/dev/null; sleep 1
sudo /tmp/openairinterface5g/cmake_targets/ran_build/build/nr-softmodem \
  -O /etc/oai_gnb/gnb.sa.rfsim.conf \
  --rfsim \
  --log_config.global_log_options level,nocolor,time \
  2>&1 | tee /tmp/gnb2_oai.log
# Success: "Waiting for RRCSetupRequest" — gNB ready, listening on port 4043

# --- Terminal 3: Start OAI nrUE (uehost2) — RFsimulator client mode ---
ssh saish@pc818.emulab.net
sudo pkill -9 nr-uesoftmodem 2>/dev/null; sleep 1
sudo /tmp/openairinterface5g/cmake_targets/ran_build/build/nr-uesoftmodem \
  -O /etc/oai_ue/ue.conf \
  --rfsim \
  --rfsimulator.serveraddr 10.10.1.3 \
  --rfsimulator.serverport 4043 \
  --log_config.global_log_options level,nocolor,time \
  2>&1 | tee /tmp/ue2_oai.log
# Success: oaitun_ue1 interface appears with 10.45.x.x address

# Verify
ip addr show oaitun_ue1
```

---

## Step 9 — Ping & iPerf3 Tests

### Experiment A — srsRAN (ue1 netns)

```bash
# Start iPerf3 server on core
ssh saish@pc808.emulab.net 'iperf3 -s -D'

# Ping UE → Core UPF gateway
ssh saish@pc801.emulab.net 'sudo ip netns exec ue1 ping 10.45.0.1 -c 5'
# Expected: ~35-45 ms RTT, 0% packet loss

# TCP throughput (uplink, 30s)
ssh saish@pc801.emulab.net \
  'sudo ip netns exec ue1 iperf3 -c 10.10.1.1 -t 30 -i 5'

# UDP 10 Mbps (uplink, 30s)
ssh saish@pc801.emulab.net \
  'sudo ip netns exec ue1 iperf3 -c 10.10.1.1 -u -b 10M -t 30 -i 5'

ssh saish@pc808.emulab.net 'pkill iperf3'
```

### Experiment B — OAI (oaitun_ue1 interface)

```bash
ssh saish@pc808.emulab.net 'iperf3 -s -D'

# Ping OAI UE → Core
ssh saish@pc818.emulab.net 'ping 10.45.0.1 -I oaitun_ue1 -c 5'

# TCP throughput (uplink, 30s)
ssh saish@pc818.emulab.net \
  'iperf3 -c 10.10.1.1 -B $(ip addr show oaitun_ue1 | grep "inet " | awk "{print \$2}" | cut -d/ -f1) -t 30 -i 5'

ssh saish@pc808.emulab.net 'pkill iperf3'
```

---

## Stopping the Network

```bash
# Stop all gNB and UE processes
ssh saish@pc802.emulab.net 'sudo pkill -9 gnb 2>/dev/null || true'
ssh saish@pc801.emulab.net 'sudo pkill -9 srsue 2>/dev/null; sudo ip netns exec ue1 ip link del tun_srsue 2>/dev/null || true'
ssh saish@pc811.emulab.net 'sudo pkill -9 nr-softmodem 2>/dev/null || true'
ssh saish@pc818.emulab.net 'sudo pkill -9 nr-uesoftmodem 2>/dev/null || true'

# Core services remain running between experiments
# To restart core cleanly:
ssh saish@pc808.emulab.net 'sudo systemctl restart open5gs-nrfd open5gs-amfd open5gs-smfd open5gs-upfd'
```

---

## Troubleshooting

### ❌ AMF crashes: `No amf.time.t3512.value`
- **Cause:** Open5GS 2.8.0 requires the `time:` block in `amf.yaml` — it crashes without it even though the default package config omits it.
- **Fix:** Use the provided `core/amf.yaml` which includes `t3502`, `t3512`, and `n1` timers.

### ❌ srsRAN Project: `git clone` gives only README + archive branch
- **Cause:** The default branch of `srsran/srsRAN_Project` is now `archive`.
- **Fix:**
  ```bash
  git fetch origin main:main --depth=1
  git checkout main
  ```

### ❌ srsUE compile: `ccache permission denied`
- **Cause:** A previous root build created `~/.cache/ccache` owned by root.
- **Fix:**
  ```bash
  sudo chown -R saish: ~/.cache/ccache 2>/dev/null; rm -rf ~/.cache/ccache
  CCACHE_DISABLE=1 make -j$(nproc) srsue
  ```

### ❌ OAI build: `generator Ninja does not match Unix Makefiles`
- **Cause:** Stale root-owned `ran_build/` from a previous build attempt.
- **Fix:**
  ```bash
  sudo rm -rf /tmp/openairinterface5g/cmake_targets/ran_build
  ./build_oai --gNB --noavx512
  ```

### ❌ OAI build fails: `Permission denied` on cmake files
- **Cause:** Previous build ran as root, locking the build directory.
- **Fix:** Same as above — `sudo rm -rf ran_build` before retrying.

### ❌ srsUE: `PDU Session Establishment failed` / no IP assigned
- Check subscriber is in MongoDB with matching IMSI/K/OPC
- Check APN name (`srsapn` for srsUE, `oai` for OAI nrUE)
- Check `sudo ip netns add ue1` was run
- Review AMF log: `sudo tail -30 /var/log/open5gs/amf.log`

### ❌ srsUE: ZMQ port `Address already in use`
- Kill stale gNB: `sudo pkill -9 gnb` on gnb1, wait 3s before restarting.

### ❌ OAI nrUE: RFsimulator connection refused
- gnb2 must be running and in "waiting" state before nrUE connects.
- Check: `ssh saish@pc811.emulab.net 'ss -tnlp | grep 4043'`

---

## Key Parameters Reference

### PLMN / Radio

| Parameter | Experiment A (srsRAN) | Experiment B (OAI) |
|---|---|---|
| MCC / MNC | 001 / 01 | 001 / 01 |
| TAC | 7 | 7 |
| NR Band | n3 (FDD, 1842.5 MHz DL) | n78 (TDD, 3.5 GHz) |
| DL ARFCN / SSB ARFCN | 368500 | 641272 |
| Bandwidth | 10 MHz (52 PRBs) | 20 MHz (106 PRBs) |
| Subcarrier Spacing | 15 kHz (FDD) | 30 kHz (TDD) |
| Virtual RF | ZMQ TCP | RFsimulator TCP |
| ZMQ / RFsim port | gnb1: 2000 (DL), 2001 (UL) | gnb2: 4043 |

### Subscriber Credentials

| | Experiment A (srsUE) | Experiment B (OAI nrUE) |
|---|---|---|
| IMSI | 001010123456780 | 001010000000001 |
| K | 00112233445566778899aabbccddeeff | fec86ba6eb707ed08905757b1bb44b8f |
| OPC | 63bfa50ee6523365ff14c1f45f88737d | C42449363BBAD02B66D16BC975D77CC1 |
| Algorithm | Milenage | Milenage |
| APN / DNN | srsapn | oai |
| Slice | SST=1 | SST=1 |

### Open5GS 5GC Network Addresses

| Component | Address | Transport |
|---|---|---|
| AMF NGAP (N2) | 10.10.1.1:38412 | SCTP |
| UPF GTP-U (N3) | 10.10.1.1:2152 | UDP |
| UPF PFCP | 127.0.0.7:8805 | UDP |
| NRF SBI | 127.0.0.10:7777 | HTTP |
| AMF SBI | 127.0.0.5:7777 | HTTP |
| SMF SBI | 127.0.0.4:7777 | HTTP |
| UE PDN pool | 10.45.0.0/16 | — (ogstun) |
| UE PDN GW | 10.45.0.1 | — (ogstun) |

---

## Repository Structure

```
5g-experiment/
├── README.md                       ← This file (with full version table)
├── core/
│   ├── amf.yaml                    ← Open5GS 2.8.0 AMF (NGAP on 10.10.1.1, t3512 fix)
│   └── upf.yaml                    ← Open5GS 2.8.0 UPF (GTP-U on 10.10.1.1)
├── gnb1/
│   └── gnb_zmq.yaml                ← srsRAN Project 25.10.0 gNB (ZMQ, Band n3)
├── uehost1/
│   └── ue_zmq.conf                 ← srsRAN 4G 23.04 srsUE (NR/ZMQ, IMSI 001010123456780)
├── gnb2/
│   └── gnb.sa.rfsim.conf           ← OAI 2026.w40 nr-softmodem (RFsim, Band n78)
└── uehost2/
    └── ue.conf                     ← OAI 2026.w40 nrUE (RFsim, IMSI 001010000000001)
```
