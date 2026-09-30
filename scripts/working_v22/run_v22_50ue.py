#!/usr/bin/env python3
"""
run_v22_50ue.py — v22.10 50-UE accumulation experiment on POWDER.
================================================================
Node mapping:
  core    = pc808   10.10.1.1   Open5GS EPC (MME+SGW+UPF)
  gnb1    = pc802   10.10.1.2   srsenb — one process per UE
  uehost1 = pc801   10.10.1.4   srsue  — one process per UE

Config paths (installed on nodes):
  gNB:  /etc/srsenb_v22/enb_ue{n}.conf
        ZMQ tx=tcp://*:4{nn}0  rx=tcp://10.10.1.4:4{nn}1
  UE:   /etc/srsue_v22/ue{n}.conf
        netns=ue{n}  tun=tun_ue{n}

Port formula: UE n → base_port = 40000 + n*10
  gNB tx = base_port     (DL to UE)
  gNB rx = base_port + 1 (UL from UE)

Output: results/ver_eval/v22_10/ue_results_50.csv  — 170-column full schema
  - 18 DL + 18 UL rates (1-10, 15,20,...,50 Mbps) + loss_pct
  - RAN params (mcs, brate, prb, tbs, bler, cqi, sinr, phr, nof_ue, system_load)
  - turbostat: 18 columns
  - RAPL pkg0+pkg1 W
  - system: loadavg, cpu_freq, srsenb_cpu_pct
  - phy_*   : UE-side srsue metrics CSV averages (16 cols)
  - gnb_*   : gNB metrics CSV window aggregates  (9 cols)
  - gnbj_*  : gNB report JSON window aggregates  (17 cols)

Experiment design (accumulation — all previous UEs stay running):
  PHASE 1 (1..MEASURE_FROM-1): fast-launch gNB+UE, wait attach, no measurements
  PHASE 2 (MEASURE_FROM..50):  full ping + 18-rate DL/UL iperf + snapshots per UE

Key fixes:
  - EPC restarted BEFORE gNBs (gNBs must connect to current MME pid)
  - turbostat output written to tmpfile (avoids stdin conflict in bash -s pipe)
  - RAPL read via sudo cat (avoids permission denied)
  - heal_upf(): auto-detects UPF spoofing DROP and restarts UPF/SGW-U without
    killing any gNB/srsue process
  - Smart resume: discards bad rows (ping_loss=100%) on restart, advances
    MEASURE_FROM to last good UE+1 automatically
  - iperf3 persistent server loop (while true) so each port stays alive across
    all 18 consecutive calls per UE
  - RAN params snapped DURING UL iperf (idx_r==1) via background thread so
    scheduler has non-zero MCS/PRB/SINR/PHR while traffic flows
  - phy_* pulled from srsue metrics CSV accumulated over the iperf window
  - gnbj_* pulled from gNB report JSON accumulated over the iperf window
  - gnb agg cols pulled from gNB metrics CSV window
"""

import subprocess, time, csv, os, re, json, pathlib, hashlib, shutil, signal, threading

# ── Node config ───────────────────────────────────────────────────────────────
KEY      = os.path.expanduser("~/.ssh/id_ed25519")
GNB_HOST = "saish@pc802.emulab.net"
UE_HOST  = "saish@pc801.emulab.net"
CORE_HOST= "saish@pc808.emulab.net"

SRSENB_BIN   = "/opt/srsRAN_src/build/srsenb/src/srsenb"
SRSUE_BIN    = "/opt/srsRAN_src/build/srsue/src/srsue"
ENB_CONF_DIR = "/etc/srsenb_v22"
UE_CONF_DIR  = "/etc/srsue_v22"
UE_LOG_DIR   = "/tmp/ue_logs"
GNB_LOG_DIR  = "/tmp/gnb_logs"

# GTP fix: each srsenb binds to a unique IP alias on enp4s0f1 so SGW-U
# delivers DL GTP packets to the correct process (not a random one of 50).
# Aliases 10.10.2.1-50 must exist on pc802/enp4s0f1; route 10.10.2.0/24 via
# 10.10.1.2 must exist on pc808 for the SGW-U → gNB GTP DL path.
GNB_GTP_BASE = "10.10.2"   # gtp_bind_addr = 10.10.2.{n}
GNB_LAN_IF   = "enp4s0f1"  # physical LAN interface on pc802

ATTACH_TIMEOUT = 600      # seconds — 600s attach timeout
PING_COUNT     = 20
PING_SETTLE_S  = 5      # wait after injecting default route before pinging
DL_RATES = list(range(1, 11)) + list(range(15, 51, 5))   # 18 rates
UL_RATES = list(range(1, 11)) + list(range(15, 51, 5))   # 18 rates
IPERF_DUR      = 10
MAX_UE         = 50
MEASURE_FROM   = 1      # Measure all UEs from 1 to 50
CORE_IP        = "10.45.0.1"

OUT_DIR  = pathlib.Path("results/ver_eval/v22_10")
OUT_FILE = OUT_DIR / "ue_results_50.csv"

SSH_ARGS = ["-i", KEY,
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=20",
            "-o", "ServerAliveCountMax=3",
            "-o", "BatchMode=yes"]


# ── SSH helpers ───────────────────────────────────────────────────────────────
def ssh(host, cmd, timeout=30):
    """Run cmd via bash -s (bypasses tcsh default shell on Emulab)."""
    args = ["ssh"] + SSH_ARGS + [host, "bash -s"]
    try:
        r = subprocess.run(args, input=cmd, capture_output=True,
                           text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "TIMEOUT", -1
    except Exception as e:
        return f"SSH_ERR:{e}", -1


def ssh_bg(host, cmd):
    """Upload + launch cmd fully detached from SSH (double-fork via setsid)."""
    tag     = hashlib.md5(cmd.encode()).hexdigest()[:8]
    rscript = f"/tmp/bg_{tag}.sh"
    upload  = ["ssh"] + SSH_ARGS + [host, f"cat > {rscript} && chmod +x {rscript}"]
    subprocess.run(upload, input=f"#!/bin/bash\n{cmd}\n",
                   capture_output=True, text=True, timeout=12)
    launcher = f"setsid bash {rscript} </dev/null >/dev/null 2>&1 & disown $! && exit 0"
    subprocess.run(["ssh"] + SSH_ARGS + [host, f"bash -c '{launcher}'"],
                   capture_output=True, text=True, timeout=12)
    time.sleep(0.5)


def kill_all():
    """
    Kill all srsenb/srsue processes.
    SIGTERM first so srsue sends NAS Detach → MME cleans PFCP session + TEID.
    Hard SIGKILL leaves stale UPF TEID entries → spoofing DROPs on next run.
    """
    ssh(GNB_HOST, "sudo killall srsenb 2>/dev/null; sleep 1; "
                  "sudo killall -9 srsenb 2>/dev/null; sleep 1; echo done", timeout=15)
    ssh(UE_HOST,  "sudo killall srsue  2>/dev/null; sleep 4; "
                  "sudo killall -9 srsue  2>/dev/null; sleep 1; echo done", timeout=15)
    time.sleep(5)


def restart_epc():
    """
    Full EPC restart in correct order so the SMF IP pool resets to .2.
    Order: SMF → UPF → SGW-U → SGW-C → MME.
    All gNBs must be dead before this call.
    """
    print("  Restarting EPC (SMF→UPF→SGW-U→SGW-C→MME)...")
    script = """
sudo systemctl restart open5gs-smfd  && sleep 3
sudo systemctl restart open5gs-upfd   && sleep 2
sudo systemctl restart open5gs-sgwud  && sleep 2
sudo systemctl restart open5gs-sgwcd  && sleep 2
sudo systemctl restart open5gs-mmed   && sleep 4
systemctl is-active open5gs-mmed open5gs-smfd open5gs-sgwcd open5gs-sgwud open5gs-upfd
"""
    out, _ = ssh(CORE_HOST, script, timeout=30)
    statuses = [l.strip() for l in out.splitlines() if l.strip() in ("active", "inactive", "failed")]
    all_active = all(s == "active" for s in statuses)
    print(f"  EPC restart {'OK' if all_active else 'WARN'}: {statuses}")
    return all_active


# ── gNB: start one srsenb for UE n ───────────────────────────────────────────
def start_gnb_ue(n):
    conf    = f"{ENB_CONF_DIR}/enb_ue{n}.conf"
    log     = f"{GNB_LOG_DIR}/enb_ue{n}.log"
    met_csv = f"{GNB_LOG_DIR}/enb_ue{n}_metrics.csv"
    rpt_json= f"{GNB_LOG_DIR}/enb_ue{n}_report.json"
    ssh(GNB_HOST,
        f"mkdir -p {GNB_LOG_DIR} && rm -f {log} {met_csv} {rpt_json}",
        timeout=10)
    ssh_bg(GNB_HOST,
           f"sudo {SRSENB_BIN} {conf}"
           f" --expert.metrics_csv_enable=1"
           f" --expert.metrics_csv_filename={met_csv}"
           f" --expert.metrics_period_secs=1"
           f" --expert.report_json_enable=1"
           f" --expert.report_json_filename={rpt_json}"
           f" >> {log} 2>&1")


def wait_gnb_port(n, timeout=25):
    """Wait for gNB ZMQ tx port 40000+n*10 to bind."""
    port     = 40000 + n * 10
    deadline = time.time() + timeout
    while time.time() < deadline:
        out, _ = ssh(GNB_HOST,
                     f"ss -tnlp 2>/dev/null | grep -c ':{port} ' || echo 0",
                     timeout=10)
        if out.strip() == "1":
            return True
        time.sleep(2)
    print(f"  WARNING: gNB port {port} not seen after {timeout}s")
    return False


# ── UE helpers ────────────────────────────────────────────────────────────────
def inject_default_route(n):
    """
    Fix routing in netns ueN so ping/iperf to 10.45.0.1 (ogstun) works.

    Root cause: srsue assigns tun_ueN IP as 10.45.0.x/24 which creates a
    connected route 10.45.0.0/24 dev tun_ueN.  When the kernel sees
    ping→10.45.0.1 it matches the /24 connected route and tries ARP on the
    P2P TUN device — ARP never replies → 100% loss.

    Fix: replace the /24 connected route with:
      1. 10.45.0.1/32 dev tun_ueN  (explicit host route for the gateway)
      2. default via 10.45.0.1 dev tun_ueN  (all other traffic)
    This forces the kernel to send to the gateway via the TUN without ARP.
    """
    cmd = (
        f"TUN=$(sudo ip netns exec ue{n} ip -br a 2>/dev/null "
        f"  | grep -i tun | awk '{{print $1}}' | head -1); "
        f"if [ -n \"$TUN\" ]; then "
        f"  sudo ip netns exec ue{n} ip route del 10.45.0.0/24 dev $TUN 2>/dev/null || true; "
        f"  sudo ip netns exec ue{n} ip route add 10.45.0.1/32 dev $TUN 2>/dev/null || true; "
        f"  sudo ip netns exec ue{n} ip route add default via 10.45.0.1 dev $TUN 2>/dev/null || true; "
        f"  sudo ip netns exec ue{n} ip route show 2>/dev/null; "
        f"fi"
    )
    out, _ = ssh(UE_HOST, cmd, timeout=12)
    return "10.45.0.1" in out and "default" in out


def setup_gtp_aliases(max_n=50):
    """
    Ensure each srsenb slot n gets its own GTP-U bind IP 10.10.2.n on pc802,
    and that pc808 has a route to 10.10.2.0/24 via 10.10.1.2.

    This prevents the DL GTP delivery problem: when N srsenb processes all
    bind to the same 10.10.1.2:2152, the kernel delivers incoming GTP packets
    to an arbitrary process.  With unique bind IPs, the SGW-U sends each UE's
    DL to the exact srsenb that owns its TEID.

    Also patches all enb_ue{n}.conf files to set gtp_bind_addr = 10.10.2.{n}.
    """
    print(f"  Setting up GTP alias IPs 10.10.2.1-{max_n} on pc802 (enp4s0f1)...")
    alias_script = f"for n in $(seq 1 {max_n}); do sudo ip addr add {GNB_GTP_BASE}.$n/24 dev {GNB_LAN_IF} 2>/dev/null || true; done; echo count=$(ip addr show {GNB_LAN_IF} | grep -c '{GNB_GTP_BASE}\.' || echo 0)"
    out, _ = ssh(GNB_HOST, alias_script, timeout=30)
    print(f"  {out.strip()}")

    print(f"  Patching enb_ue*.conf: gtp_bind_addr → 10.10.2.n ...")
    patch_script = (
        f"for n in $(seq 1 {max_n}); do "
        f"  sudo sed -i \"s/^gtp_bind_addr.*/gtp_bind_addr = {GNB_GTP_BASE}.$n/\" "
        f"  /etc/srsenb_v22/enb_ue${{n}}.conf 2>/dev/null; "
        f"done; "
        f"echo patched"
    )
    out2, _ = ssh(GNB_HOST, patch_script, timeout=30)
    print(f"  {out2.strip()}")

    print(f"  Adding route 10.10.2.0/24 via 10.10.1.2 on pc808...")
    route_script = (
        f"sudo ip route add {GNB_GTP_BASE}.0/24 via 10.10.1.2 dev enp4s0f1 2>/dev/null || "
        f"sudo ip route change {GNB_GTP_BASE}.0/24 via 10.10.1.2 dev enp4s0f1 2>/dev/null || true; "
        f"echo route=$(ip route show | grep '{GNB_GTP_BASE}' | head -1)"
    )
    out3, _ = ssh(CORE_HOST, route_script, timeout=15)
    print(f"  {out3.strip()}")


def precreate_netns(max_n=50):
    """Pre-create all network namespaces ue1..ue{max_n} on UE host."""
    cmd = (f"for n in $(seq 1 {max_n}); do "
           f"  sudo ip netns add ue$n 2>/dev/null || true; "
           f"done; "
           f"echo \"netns_count:$(ip netns list | wc -l)\"")
    out, _ = ssh(UE_HOST, cmd, timeout=30)
    count = 0
    for line in out.splitlines():
        if line.startswith("netns_count:"):
            try: count = int(line.split(":")[1])
            except Exception: pass
    print(f"  Pre-created {count} network namespaces (ue1..ue{max_n})")


def start_srsue(n):
    conf    = f"{UE_CONF_DIR}/ue{n}.conf"
    log     = f"{UE_LOG_DIR}/ue{n}.log"
    ue_csv  = f"{UE_LOG_DIR}/ue{n}_metrics.csv"
    ctx_dir = f"/tmp/ue_ctx/ue{n}"
    ssh(UE_HOST,
        f"mkdir -p {UE_LOG_DIR} {ctx_dir} && "
        f"rm -f {log} {ue_csv} {ctx_dir}/.ctxt /users/saish/.ctxt /root/.ctxt 2>/dev/null; true",
        timeout=10)
    ssh_bg(UE_HOST,
           f"cd {ctx_dir} && "
           f"sudo HOME={ctx_dir} {SRSUE_BIN} {conf}"
           f" --general.metrics_csv_enable=1"
           f" --general.metrics_csv_filename={ue_csv}"
           f" --general.metrics_period_secs=1"
           f" >> {log} 2>&1")


def wait_attach(n, timeout=ATTACH_TIMEOUT):
    """Poll for tun interface in netns ue{n}. Returns (ip, elapsed_ms)."""
    t_start  = time.time()
    deadline = t_start + timeout
    time.sleep(6)
    while time.time() < deadline:
        out, _ = ssh(UE_HOST,
                     f"sudo ip netns exec ue{n} ip -br a 2>/dev/null"
                     f" | grep tun | awk '{{print $3}}'",
                     timeout=12)
        ip = out.strip().split("/")[0] if out.strip() else None
        if ip:
            return ip, int((time.time() - t_start) * 1000)
        time.sleep(3)
    return None, int(timeout * 1000)


# ── Tests ─────────────────────────────────────────────────────────────────────
def run_ping(n, count=None):
    c   = count or PING_COUNT
    cmd = (f"sudo ip netns exec ue{n} ping -c {c} -i 0.3 -W 2 {CORE_IP}")
    out, _ = ssh(UE_HOST, cmd, timeout=c * 2 + 12)
    try:
        loss_m = re.search(r'(\d+(?:\.\d+)?)% packet loss', out)
        loss   = float(loss_m.group(1)) if loss_m else 100.0
        rtt_m  = re.search(
            r'rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)', out)
        if rtt_m:
            return (float(rtt_m.group(2)), float(rtt_m.group(1)),
                    float(rtt_m.group(3)), float(rtt_m.group(4)), loss)
    except Exception:
        pass
    return 999.0, 999.0, 999.0, 999.0, 100.0


def heal_upf(ue_n):
    """
    Full re-attach cycle when a UE shows 100% ping loss after attaching.

    Root cause: restarting EPC services while the UE tun is already up wipes
    the UPF PFCP session — the UE's IP tunnel becomes a black hole.  Simply
    restarting services in-place does NOT recover connectivity; the UE must
    re-negotiate the PDU session.

    Strategy:
      1. Kill just this UE's srsue + its gNB slot.
      2. Full EPC restart (resets SMF pool + UPF PFCP state cleanly).
      3. Restart the gNB slot for ue_n.
      4. Re-start srsue for ue_n and wait for fresh attach.
      5. Confirm with ping.

    Returns (recovered: bool, new_ue_ip: str, new_attach_ms: int).
    All gNBs for UEs 1..ue_n-1 remain running throughout.
    """
    print(f"  [HEAL] 100% ping loss on UE{ue_n} — starting full re-attach cycle...")

    # Step 1: kill just this UE's processes
    print(f"  [HEAL] Step 1/4: killing srsue{ue_n} + srsenb{ue_n}...")
    ssh(UE_HOST,
        f"sudo pkill -9 -f 'srsue.*ue{ue_n}\\.conf' 2>/dev/null; sleep 1; echo done",
        timeout=12)
    ssh(GNB_HOST,
        f"sudo pkill -9 -f 'srsenb.*enb_ue{ue_n}\\.conf' 2>/dev/null; sleep 1; echo done",
        timeout=12)
    time.sleep(3)

    # Step 2: full EPC restart so SMF IP pool and UPF PFCP state are clean
    print(f"  [HEAL] Step 2/4: full EPC restart...")
    heal_script = """
sudo systemctl restart open5gs-smfd   && sleep 3
sudo systemctl restart open5gs-upfd   && sleep 2
sudo systemctl restart open5gs-sgwud  && sleep 2
sudo systemctl restart open5gs-sgwcd  && sleep 2
sudo systemctl restart open5gs-mmed   && sleep 4
systemctl is-active open5gs-mmed open5gs-smfd open5gs-sgwcd open5gs-sgwud open5gs-upfd
"""
    out, _ = ssh(CORE_HOST, heal_script, timeout=30)
    statuses = [l.strip() for l in out.splitlines()
                if l.strip() in ("active", "inactive", "failed")]
    all_ok = all(s == "active" for s in statuses)
    print(f"  [HEAL] EPC services: {statuses} ({'OK' if all_ok else 'WARN'})")

    # Step 3: restart the gNB slot for ue_n
    print(f"  [HEAL] Step 3/4: restarting gNB slot {ue_n}...")
    start_gnb_ue(ue_n)
    if not wait_gnb_port(ue_n, timeout=25):
        print(f"  [HEAL] gNB port not bound — heal failed")
        return False, "", 0

    # Step 4: re-attach srsue
    print(f"  [HEAL] Step 4/4: re-attaching srsue{ue_n} (timeout={ATTACH_TIMEOUT}s)...")
    # Clear stale NAS context so UE does a fresh RRC connection
    ctx_dir = f"/tmp/ue_ctx/ue{ue_n}"
    ssh(UE_HOST,
        f"rm -f {ctx_dir}/.ctxt /users/saish/.ctxt /root/.ctxt 2>/dev/null; "
        f"sudo ip netns exec ue{ue_n} ip link set lo up 2>/dev/null; true",
        timeout=10)
    start_srsue(ue_n)
    time.sleep(2)

    new_ip, new_ms = wait_attach(ue_n)
    if not new_ip:
        print(f"  [HEAL] Re-attach timed out — heal failed")
        return False, "", new_ms

    print(f"  [HEAL] Re-attached: IP={new_ip}  {new_ms}ms")

    # Inject default route — required after fresh attach
    inject_default_route(ue_n)
    time.sleep(PING_SETTLE_S)

    # Confirm ping
    p_avg, _, _, _, p_loss = run_ping(ue_n, count=5)
    print(f"  [HEAL] Post-heal ping: avg={p_avg}ms loss={p_loss}%")
    if p_loss < 50.0:
        print(f"  [HEAL] ✓ RECOVERED")
        return True, new_ip, new_ms
    print(f"  [HEAL] Ping still failing after re-attach — heal failed")
    return False, new_ip, new_ms


def ensure_iperf_server(n):
    """Ensure a persistent looping iperf3 server is running on CORE_HOST."""
    port = 5200 + n
    out, _ = ssh(CORE_HOST,
                 f"ss -tnlp 2>/dev/null | grep -c ':{port} ' || echo 0",
                 timeout=8)
    if out.strip() == "1":
        return
    loop = (f"while true; do "
            f"iperf3 -s -B {CORE_IP} -p {port} --one-off 2>/dev/null; "
            f"sleep 0.2; done")
    ssh_bg(CORE_HOST, loop)
    time.sleep(1)


def run_iperf(n, rate_mbps, direction="dl"):
    """Run iperf3 from UE n netns. Returns (mbps, loss_pct)."""
    ensure_iperf_server(n)
    port = 5200 + n
    flag = "-R" if direction == "dl" else ""
    cmd  = (f"sudo ip netns exec ue{n} iperf3"
            f" -c {CORE_IP} -p {port}"
            f" -b {rate_mbps}M -t {IPERF_DUR} {flag} --json")
    out, _ = ssh(UE_HOST, cmd, timeout=IPERF_DUR + 15)
    if out == "TIMEOUT":
        return 0.0, 100.0
    try:
        dec    = json.JSONDecoder()
        last_d = None
        i = 0
        while i < len(out):
            try:
                obj, idx = dec.raw_decode(out, i)
                if isinstance(obj, dict) and "end" in obj:
                    last_d = obj
                i = idx
            except json.JSONDecodeError:
                i += 1
        if last_d is None or last_d.get("error"):
            return 0.0, 100.0
        end  = last_d["end"]
        key  = "sum_received" if direction == "dl" else "sum_sent"
        mbps = round(end[key]["bits_per_second"] / 1e6, 4)
        return mbps, 0.0
    except Exception:
        return 0.0, 100.0


# ── gNB metrics snapshot (turbostat + RAPL + loadavg + cpu_freq + srsenb%) ───
def snap_gnb_metrics():
    """
    Collect from pc802 (gnb1):
      - turbostat 18 columns (5 s sample) — written to tmpfile (no stdin conflict)
        Uses --show flag + picks the LAST non-empty data row (package-level summary).
        Row 3 (sed -n '3p') is a per-core row on multi-socket; last row is the
        package total.  Columns absent on this hardware are silently zeroed.
      - RAPL pkg0 + pkg1 (dual-socket) via sudo cat
      - /proc/loadavg
      - cpu_freq avg + max across all cores
      - cpu user% + sys% from /proc/stat
      - temperature (max coretemp)
      - srsenb aggregate cpu% + total RSS
    """
    script = r"""
TS_COLS="Avg_MHz,Busy%,Bzy_MHz,IPC,IRQ,SMI,CPU%c1,CPU%c3,CPU%c6,CPU%c7,CoreTmp,PkgTmp,Pkg%pc2,Pkg%pc3,PkgWatt,RAMWatt,PKG_%,RAM_%"
TSFILE=$(mktemp /tmp/ts_XXXXXX)
sudo turbostat --quiet --show $TS_COLS sleep 5 > $TSFILE 2>&1
# turbostat output structure:
#   line 1: "X.XXX sec"  (timing)
#   line 2: header (Avg_MHz  Busy%  ...)
#   line 3: PACKAGE-LEVEL SUMMARY (all 18 fields, tabs between) ← we want this
#   line 4+: per-core rows (often truncated, missing PkgWatt etc.)
# Strategy: skip the timing line and header, take the FIRST full data row.
# "Full" = 15+ tab-separated fields (package rows always have all columns).
TS_OUT=$(grep -v '^$' "$TSFILE" \
  | grep -v '^[A-Za-z]' \
  | grep -v '^[0-9][0-9.]*[[:space:]]*sec' \
  | awk -F'\t' 'NF>=15{print; exit}')
# Fallback: first non-empty, non-header, non-timing data line
if [ -z "$TS_OUT" ]; then
  TS_OUT=$(grep -v '^$' "$TSFILE" | grep -v '^[A-Za-z]' | grep -v '^[0-9][0-9.]*[[:space:]]*sec' | head -1)
fi
rm -f $TSFILE
echo "TS:$TS_OUT"

for pkg in 0 1; do
  P="/sys/class/powercap/intel-rapl/intel-rapl:${pkg}/energy_uj"
  if [ -f "$P" ]; then
    E1=$(sudo cat "$P"); sleep 1; E2=$(sudo cat "$P")
    echo "RAPL${pkg}:$(echo "scale=3; ($E2 - $E1) / 1000000" | bc)"
  else
    echo "RAPL${pkg}:0"
  fi
done

awk '{print "LOAD:"$1"/"$2"/"$3}' /proc/loadavg

# CPU freq avg + max from /proc/cpuinfo
awk 'BEGIN{s=0;n=0;mx=0} /cpu MHz/{v=$4+0; s+=v; n++; if(v>mx)mx=v}
     END{if(n>0)printf "FREQ:%.0f/%.0f\n",s/n,mx}' /proc/cpuinfo

# CPU user% + sys% from /proc/stat (1s window)
C1=$(awk '/^cpu /{print $2,$3,$4,$5,$6,$7,$8}' /proc/stat)
sleep 1
C2=$(awk '/^cpu /{print $2,$3,$4,$5,$6,$7,$8}' /proc/stat)
awk -v a="$C1" -v b="$C2" 'BEGIN{
  split(a,x); split(b,y)
  usr=y[1]-x[1]; sys=y[3]-x[3]; idle=y[4]-x[4]; nice=y[2]-x[2]
  tot=usr+sys+idle+nice; if(tot==0) tot=1
  printf "CPUSTAT:%.2f:%.2f\n", usr*100/tot, sys*100/tot
}'

# Temperature — max coretemp
TEMP=$(cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | awk 'BEGIN{m=0}{v=$1/1000;if(v>m)m=v}END{printf "%.1f",m}')
echo "TEMP:$TEMP"

# srsenb aggregate cpu% + RSS KB
ps aux | awk '/srsenb/{if(!/grep/){cpu+=$3; rss+=$6; n++}} END{printf "PROC:%.2f:%.0f\n",cpu,rss}'
"""
    out, _ = ssh(GNB_HOST, script, timeout=22)
    row = {
        "gnb_ts_avg_mhz": 0.0, "gnb_ts_busy_pct": 0.0, "gnb_ts_bzy_mhz": 0.0,
        "gnb_ts_ipc": 0.0, "gnb_ts_irq": 0.0, "gnb_ts_smi": 0.0,
        "gnb_ts_c1_pct": 0.0, "gnb_ts_c3_pct": 0.0, "gnb_ts_c6_pct": 0.0,
        "gnb_ts_c7_pct": 0.0, "gnb_ts_core_tmp": 0.0, "gnb_ts_pkg_tmp": 0.0,
        "gnb_ts_pkg_pc2_pct": 0.0, "gnb_ts_pkg_pc3_pct": 0.0,
        "gnb_ts_pkg_watt": 0.0, "gnb_ts_ram_watt": 0.0,
        "gnb_ts_pkg_pct": 0.0, "gnb_ts_ram_pct": 0.0,
        "gnb_rapl_pkg0_w": 0.0, "gnb_rapl_pkg1_w": 0.0, "gnb_rapl_total_w": 0.0,
        "gnb_load1": 0.0, "gnb_load5": 0.0, "gnb_load15": 0.0,
        "gnb_cpu_freq_avg_mhz": 0.0, "gnb_cpu_freq_max_mhz": 0.0,
        "gnb_cpu_user_pct": 0.0, "gnb_cpu_sys_pct": 0.0,
        "gnb_temp_c": 0.0,
        "gnb_srsenb_cpu_pct": 0.0,
        # legacy alias kept for backward-compat (same as gnb_cpu_freq_avg_mhz)
        "gnb_cpu_freq_mhz": 0.0,
    }
    TS_KEYS = [
        "gnb_ts_avg_mhz", "gnb_ts_busy_pct", "gnb_ts_bzy_mhz",
        "gnb_ts_ipc", "gnb_ts_irq", "gnb_ts_smi",
        "gnb_ts_c1_pct", "gnb_ts_c3_pct", "gnb_ts_c6_pct", "gnb_ts_c7_pct",
        "gnb_ts_core_tmp", "gnb_ts_pkg_tmp",
        "gnb_ts_pkg_pc2_pct", "gnb_ts_pkg_pc3_pct",
        "gnb_ts_pkg_watt", "gnb_ts_ram_watt",
        "gnb_ts_pkg_pct", "gnb_ts_ram_pct",
    ]
    for line in out.splitlines():
        if line.startswith("TS:"):
            parts = line[3:].split()
            for i, k in enumerate(TS_KEYS):
                if i < len(parts):
                    try: row[k] = float(parts[i])
                    except ValueError: pass
        elif line.startswith("RAPL0:"):
            try: row["gnb_rapl_pkg0_w"] = float(line.split(":")[1])
            except Exception: pass
        elif line.startswith("RAPL1:"):
            try: row["gnb_rapl_pkg1_w"] = float(line.split(":")[1])
            except Exception: pass
        elif line.startswith("LOAD:"):
            parts = line[5:].split("/")
            try:
                row["gnb_load1"]  = float(parts[0])
                row["gnb_load5"]  = float(parts[1])
                row["gnb_load15"] = float(parts[2])
            except Exception: pass
        elif line.startswith("FREQ:"):
            parts = line[5:].split("/")
            try:
                row["gnb_cpu_freq_avg_mhz"] = float(parts[0])
                row["gnb_cpu_freq_mhz"]     = float(parts[0])  # alias
                if len(parts) > 1:
                    row["gnb_cpu_freq_max_mhz"] = float(parts[1])
            except Exception: pass
        elif line.startswith("CPUSTAT:"):
            parts = line[8:].split(":")
            try:
                row["gnb_cpu_user_pct"] = float(parts[0])
                row["gnb_cpu_sys_pct"]  = float(parts[1])
            except Exception: pass
        elif line.startswith("TEMP:"):
            try: row["gnb_temp_c"] = float(line[5:])
            except Exception: pass
        elif line.startswith("PROC:"):
            parts = line[5:].split(":")
            try: row["gnb_srsenb_cpu_pct"] = float(parts[0])
            except Exception: pass
    row["gnb_rapl_total_w"] = round(row["gnb_rapl_pkg0_w"] + row["gnb_rapl_pkg1_w"], 3)
    return row


# ── gNB RAN params snapshot (point-in-time) ──────────────────────────────────
def snap_gnb_ran(n):
    """
    Parse the last line of enb_ue{n}_metrics.csv (semicolon-separated, v22)
    and enb_ue{n}_report.json for per-UE MCS/CQI/SNR/PHR/BLER/bitrate.

    v22 metrics CSV columns (semicolon-separated):
      time;nof_ue;dl_brate;ul_brate;proc_rmem;proc_rmem_kB;proc_vmem_kB;
      sys_mem;system_load;thread_count;cpu_0..cpu_N

    v22 report JSON path (cell_list → cell_container → ue_list → ue_container):
      dl_cqi, dl_mcs, ul_mcs, ul_snr, ul_phr, dl_bler, ul_bler,
      dl_bitrate, ul_bitrate
      bearer_list[0].bearer_container: dl_total_bytes, ul_total_bytes, dl_latency
    """
    script = f"""
MET="{GNB_LOG_DIR}/enb_ue{n}_metrics.csv"
RPT="{GNB_LOG_DIR}/enb_ue{n}_report.json"
if [ -f "$MET" ]; then
  echo "METRICS_HDR:$(head -1 $MET)"
  # Use last complete line — awk ensures all 10 core fields are present
  echo "METRICS_LAST:$(awk -F';' 'NF>=10 && NR>1 {{last=$0}} END{{print last}}' $MET)"
fi
if [ -f "$RPT" ]; then
  tail -c 300000 "$RPT"
fi
"""
    out, _ = ssh(GNB_HOST, script, timeout=15)

    ran = {
        "ran_dl_mcs": 0.0, "ran_ul_mcs": 0.0,
        "ran_dl_brate_mbps": 0.0, "ran_ul_brate_mbps": 0.0,
        "ran_dl_prb": 0.0, "ran_ul_prb": 0.0,
        "ran_dl_tbs": 0.0, "ran_ul_tbs": 0.0,
        "ran_dl_bler": 0.0, "ran_ul_bler": 0.0,
        "ran_cqi": 0.0, "ran_ri": 0,
        "ran_pusch_sinr": 0.0, "ran_phr": 0,
        "ran_nof_ue": 0, "ran_system_load": 0.0,
        "ran_proc_rmem_kb": 0, "ran_thread_count": 0,
        "ran_gnb_cpu_avg_pct": 0.0,
    }

    hdr  = None
    last = None
    json_lines = []
    for line in out.splitlines():
        if line.startswith("METRICS_HDR:"):
            # v22 metrics CSV uses semicolon separator
            hdr = line[12:].split(";")
        elif line.startswith("METRICS_LAST:"):
            last = line[13:].split(";")
        else:
            json_lines.append(line)

    # Parse metrics CSV (semicolon-separated in v22).
    # v22 metrics columns: time;nof_ue;dl_brate;ul_brate;proc_rmem;proc_rmem_kB;
    #   proc_vmem_kB;sys_mem;system_load;thread_count;cpu_0..cpu_N
    # NOTE: MCS/CQI/SINR/PHR/BLER are NOT in the metrics CSV — they are only
    # available in the report JSON (parsed below).
    if hdr and last and len(last) >= 9:
        row_map = dict(zip(hdr, last))
        def g(k):
            try: return float(row_map.get(k, 0) or 0)
            except: return 0.0
        ran["ran_dl_brate_mbps"]  = round(g("dl_brate") / 1e6, 4) if g("dl_brate") > 1 else round(g("dl_brate"), 6)
        ran["ran_ul_brate_mbps"]  = round(g("ul_brate") / 1e6, 4) if g("ul_brate") > 1 else round(g("ul_brate"), 6)
        ran["ran_nof_ue"]         = int(g("nof_ue"))
        ran["ran_system_load"]    = g("system_load")
        ran["ran_proc_rmem_kb"]   = int(g("proc_rmem_kB") or g("proc_rmem_kb") or g("rmem_kb"))
        ran["ran_thread_count"]   = int(g("thread_count") or g("nof_threads"))
        # Aggregate per-core CPU% (cpu_0..cpu_N columns)
        cpu_vals = [g(k) for k in row_map if k.startswith("cpu_") and row_map[k]]
        if cpu_vals:
            ran["ran_gnb_cpu_avg_pct"] = round(sum(cpu_vals) / len(cpu_vals), 2)

    # Parse JSON report.
    # v22 report JSON path:
    #   top-level → cell_list[0].cell_container.ue_list[0].ue_container
    # IMPORTANT: dl_mcs/ul_mcs/ul_snr/ul_phr are ONLY non-zero during active
    # traffic. We scan ALL samples and keep the LAST one with non-zero dl_bitrate
    # or ul_bitrate. dl_total_bytes/ul_total_bytes come from bearer_container.
    json_text = "\n".join(json_lines)
    dec = json.JSONDecoder()
    last_traffic_u = None   # last ue_container with active traffic
    last_u = None           # fallback: last ue_container regardless
    last_bearer = None      # cumulative bearer_container (highest bytes)
    i = 0
    while i < len(json_text):
        try:
            obj, idx = dec.raw_decode(json_text, i)
            i = idx
        except json.JSONDecodeError:
            i += 1
            continue
        if not isinstance(obj, dict) or obj.get("type") != "metrics":
            continue
        for cell in obj.get("cell_list", []):
            cc = cell.get("cell_container", {})
            ue_list = cc.get("ue_list", [])
            if not ue_list:
                continue
            u = ue_list[0].get("ue_container", {})
            if not u:
                continue
            last_u = u
            if float(u.get("dl_bitrate", 0) or 0) > 0 or float(u.get("ul_bitrate", 0) or 0) > 0:
                last_traffic_u = u
            # Track bearer with highest cumulative bytes
            for bearer in u.get("bearer_list", []):
                bc = bearer.get("bearer_container", {})
                if bc.get("qci") == 9:
                    if last_bearer is None or (
                        float(bc.get("dl_total_bytes", 0) or 0) >
                        float(last_bearer.get("dl_total_bytes", 0) or 0)
                    ):
                        last_bearer = bc

    # Prefer traffic sample; fall back to last sample for CQI
    u = last_traffic_u or last_u or {}
    if u:
        def uf(k, d=0.0):
            try: return float(u.get(k, d) or d)
            except: return d
        ran["ran_cqi"]        = uf("dl_cqi")
        ran["ran_dl_mcs"]     = uf("dl_mcs")
        ran["ran_ul_mcs"]     = uf("ul_mcs")
        ran["ran_pusch_sinr"] = uf("ul_snr") or uf("ul_pusch_snr")
        ran["ran_phr"]        = int(uf("ul_phr"))
        ran["ran_dl_bler"]    = uf("dl_bler")
        ran["ran_ul_bler"]    = uf("ul_bler")
        # Cumulative bearer bytes (total transferred this session)
        if last_bearer:
            dl_b = float(last_bearer.get("dl_total_bytes", 0) or 0)
            ul_b = float(last_bearer.get("ul_total_bytes", 0) or 0)
            if dl_b > 0:
                ran["ran_dl_brate_mbps"] = round(dl_b / 1e6, 4)
            if ul_b > 0:
                ran["ran_ul_brate_mbps"] = round(ul_b / 1e6, 4)

    return ran


# ── PHY metrics from srsue metrics CSV ───────────────────────────────────────
def snap_ue_phy(n):
    """
    Parse the last 30 rows of /tmp/ue_logs/ue{n}_metrics.csv on pc801 and
    average all PHY fields to produce the phy_* columns.

    srsue metrics CSV columns (comma-separated):
      time,dl_mcs,dl_snr,dl_rsrp,dl_rsrq,dl_pathloss,dl_brate,dl_bler,
      dl_turbo_iters,dl_cfo_hz,ul_mcs,ul_snr,ul_pucch_rssi,ul_brate,ul_bler,ul_ta_us,
      nof_active_carriers, ...

    Returns phy_* dict.
    """
    script = f"""
UE_CSV="{UE_LOG_DIR}/ue{n}_metrics.csv"
if [ -f "$UE_CSV" ]; then
  echo "PHY_HDR:$(head -1 $UE_CSV)"
  tail -30 "$UE_CSV" | grep -v '^$' | while IFS= read -r line; do echo "PHY_ROW:$line"; done
fi
"""
    out, _ = ssh(UE_HOST, script, timeout=15)

    phy = {
        "phy_dl_mcs_avg": 0.0, "phy_dl_mcs_max": 0.0,
        "phy_ul_mcs_avg": 0.0, "phy_ul_mcs_max": 0.0,
        "phy_dl_snr_avg": 0.0, "phy_dl_snr_max": 0.0,
        "phy_rsrp_avg": 0.0, "phy_pathloss_avg": 0.0,
        "phy_dl_brate_peak_mbps": 0.0, "phy_dl_brate_avg_mbps": 0.0,
        "phy_ul_brate_peak_mbps": 0.0, "phy_ul_brate_avg_mbps": 0.0,
        "phy_dl_bler_avg": 0.0, "phy_ul_bler_avg": 0.0,
        "phy_dl_turbo_avg": 0.0,
        "phy_cfo_avg": 0.0, "phy_ul_ta_avg": 0.0,
        "phy_sample_count": 0,
    }

    hdr  = None
    rows = []
    for line in out.splitlines():
        if line.startswith("PHY_HDR:"):
            # v22 srsue metrics CSV uses semicolon separator
            hdr = line[8:].split(";")
        elif line.startswith("PHY_ROW:"):
            rows.append(line[8:].split(";"))

    if not hdr or not rows:
        return phy

    def idx_of(*keys):
        for key in keys:
            for i, h in enumerate(hdr):
                if h.strip() == key:
                    return i
        return None

    def col_vals(*keys, src=None):
        """Try each key, return first non-empty list from src (default=rows)."""
        source = src if src is not None else rows
        for key in keys:
            i = idx_of(key)
            if i is None: continue
            vals = []
            for r in source:
                if i < len(r):
                    try:
                        v = float(r[i])
                        vals.append(v)
                    except (ValueError, TypeError):
                        pass
            if vals: return vals
        return []

    def safe_avg(vals): return round(sum(vals) / len(vals), 4) if vals else 0.0
    def safe_max(vals): return round(max(vals), 4) if vals else 0.0

    # Split into traffic rows (dl_brate or ul_brate > 0) — MCS is 0 when idle
    dl_bi = idx_of("dl_brate"); ul_bi = idx_of("ul_brate")
    traffic_rows = []
    for r in rows:
        def _to_f(val):
            try: return float(val) if val else 0.0
            except (ValueError, TypeError): return 0.0
        dl_b = _to_f(r[dl_bi]) if dl_bi is not None and dl_bi < len(r) else 0.0
        ul_b = _to_f(r[ul_bi]) if ul_bi is not None and ul_bi < len(r) else 0.0
        if dl_b > 0 or ul_b > 0:
            traffic_rows.append(r)

    # v22 srsue column names: rsrp (not dl_rsrp), pl (not dl_pathloss),
    #   dl_turbo (not dl_turbo_iters), cfo (not dl_cfo_hz), ul_ta (not ul_ta_us)
    dl_mcs   = col_vals("dl_mcs", src=traffic_rows)
    ul_mcs   = col_vals("ul_mcs", src=traffic_rows)
    phy["phy_dl_mcs_avg"] = safe_avg(dl_mcs);  phy["phy_dl_mcs_max"] = safe_max(dl_mcs)
    phy["phy_ul_mcs_avg"] = safe_avg(ul_mcs);  phy["phy_ul_mcs_max"] = safe_max(ul_mcs)

    dl_snr   = col_vals("dl_snr");                phy["phy_dl_snr_avg"] = safe_avg(dl_snr); phy["phy_dl_snr_max"] = safe_max(dl_snr)
    rsrp     = col_vals("rsrp", "dl_rsrp");        phy["phy_rsrp_avg"]    = safe_avg(rsrp)
    pathloss = col_vals("pl", "dl_pathloss");       phy["phy_pathloss_avg"]= safe_avg(pathloss)

    dl_brate_mbps = [v / 1e6 for v in col_vals("dl_brate") if v > 0]
    phy["phy_dl_brate_peak_mbps"] = safe_max(dl_brate_mbps)
    phy["phy_dl_brate_avg_mbps"]  = safe_avg(dl_brate_mbps)

    ul_brate_mbps = [v / 1e6 for v in col_vals("ul_brate") if v > 0]
    phy["phy_ul_brate_peak_mbps"] = safe_max(ul_brate_mbps)
    phy["phy_ul_brate_avg_mbps"]  = safe_avg(ul_brate_mbps)

    phy["phy_dl_bler_avg"] = safe_avg(col_vals("dl_bler", src=traffic_rows))
    phy["phy_ul_bler_avg"] = safe_avg(col_vals("ul_bler", src=traffic_rows))

    turbo = col_vals("dl_turbo", "dl_turbo_iters", src=traffic_rows)
    phy["phy_dl_turbo_avg"] = safe_avg(turbo)
    cfo   = col_vals("cfo", "dl_cfo_hz");  phy["phy_cfo_avg"]   = safe_avg(cfo)
    ta    = col_vals("ul_ta", "ul_ta_us"); phy["phy_ul_ta_avg"] = safe_avg(ta)
    phy["phy_sample_count"] = len(rows)

    return phy


# ── gNB metrics CSV window aggregates ────────────────────────────────────────
def snap_gnb_agg(n):
    """
    Parse the last 30 rows of enb_ue{n}_metrics.csv (comma-sep, v22) and
    compute averages/peaks to fill gnb_dl_brate_*, gnb_ul_brate_*,
    gnb_sys_load_avg, gnb_cpu_avg_pct, gnb_proc_rmem_kb_avg,
    gnb_thread_count, gnb_sample_count.
    """
    script = f"""
MET="{GNB_LOG_DIR}/enb_ue{n}_metrics.csv"
if [ -f "$MET" ]; then
  echo "GAGG_HDR:$(head -1 $MET)"
  tail -30 "$MET" | grep -v '^$' | while IFS= read -r line; do echo "GAGG_ROW:$line"; done
fi
"""
    out, _ = ssh(GNB_HOST, script, timeout=12)

    agg = {
        "gnb_dl_brate_peak_mbps": 0.0, "gnb_dl_brate_avg_mbps": 0.0,
        "gnb_ul_brate_peak_mbps": 0.0, "gnb_ul_brate_avg_mbps": 0.0,
        "gnb_sys_load_avg": 0.0, "gnb_cpu_avg_pct": 0.0,
        "gnb_proc_rmem_kb_avg": 0.0, "gnb_thread_count": 0,
        "gnb_sample_count": 0,
    }

    hdr  = None
    rows = []
    for line in out.splitlines():
        if line.startswith("GAGG_HDR:"):
            # v22 metrics CSV uses semicolon separator
            hdr = line[9:].split(";")
        elif line.startswith("GAGG_ROW:"):
            rows.append(line[9:].split(";"))

    if not hdr or not rows:
        return agg

    def col_vals(key):
        idx = None
        for i, h in enumerate(hdr):
            if h.strip() == key:
                idx = i
                break
        if idx is None:
            return []
        vals = []
        for r in rows:
            if idx < len(r):
                try:
                    v = float(r[idx])
                    vals.append(v)
                except (ValueError, TypeError):
                    pass
        return vals

    def safe_avg(vals): return round(sum(vals) / len(vals), 4) if vals else 0.0
    def safe_max(vals): return round(max(vals), 4) if vals else 0.0

    dl_b = col_vals("dl_brate")
    dl_m = [v / 1e6 for v in dl_b if v > 0]
    agg["gnb_dl_brate_peak_mbps"] = safe_max(dl_m)
    agg["gnb_dl_brate_avg_mbps"]  = safe_avg(dl_m)

    ul_b = col_vals("ul_brate")
    ul_m = [v / 1e6 for v in ul_b if v > 0]
    agg["gnb_ul_brate_peak_mbps"] = safe_max(ul_m)
    agg["gnb_ul_brate_avg_mbps"]  = safe_avg(ul_m)

    sys_load = col_vals("system_load"); agg["gnb_sys_load_avg"] = safe_avg(sys_load)

    # Aggregate per-core CPU% from cpu_0..cpu_N columns
    cpu_cols = [k for k in hdr if k.strip().startswith("cpu_")]
    all_cpu_vals = []
    for row in rows:
        row_map = dict(zip(hdr, row))
        for ck in cpu_cols:
            try:
                v = float(row_map.get(ck, 0) or 0)
                if v > 0:
                    all_cpu_vals.append(v)
            except (ValueError, TypeError):
                pass
    agg["gnb_cpu_avg_pct"] = round(sum(all_cpu_vals) / len(all_cpu_vals), 2) if all_cpu_vals else 0.0

    rmem = col_vals("proc_rmem_kB")
    if not rmem:
        rmem = col_vals("proc_rmem_kb")
    if not rmem:
        rmem = col_vals("rmem_kb")
    agg["gnb_proc_rmem_kb_avg"] = safe_avg(rmem)

    thr = col_vals("thread_count")
    if not thr:
        thr = col_vals("nof_threads")
    agg["gnb_thread_count"] = int(safe_avg(thr))
    agg["gnb_sample_count"] = len(rows)

    return agg


# ── gNB report JSON window aggregates (gnbj_*) ───────────────────────────────
def snap_gnbj(n):
    """
    Parse ALL JSON objects in enb_ue{n}_report.json and average per-UE fields
    to produce gnbj_* columns (cqi, mcs, snr, rssi, bler, bitrate, phr, latency).
    """
    script = f"""
RPT="{GNB_LOG_DIR}/enb_ue{n}_report.json"
if [ -f "$RPT" ]; then tail -c 500000 "$RPT"; fi
"""
    out, _ = ssh(GNB_HOST, script, timeout=15)

    gnbj = {
        "gnbj_dl_cqi_avg": 0.0,
        "gnbj_dl_mcs_avg": 0.0, "gnbj_dl_mcs_max": 0.0,
        "gnbj_ul_mcs_avg": 0.0, "gnbj_ul_mcs_max": 0.0,
        "gnbj_ul_snr_avg": 0.0,
        "gnbj_ul_pusch_rssi_avg": 0.0,
        "gnbj_ul_pucch_rssi_avg": 0.0,
        "gnbj_ul_pucch_ni_avg": 0.0,
        "gnbj_dl_bler_avg": 0.0, "gnbj_ul_bler_avg": 0.0,
        "gnbj_dl_bitrate_avg_mbps": 0.0, "gnbj_ul_bitrate_avg_mbps": 0.0,
        "gnbj_ul_phr_avg": 0.0,
        "gnbj_dl_total_bytes": 0.0, "gnbj_ul_total_bytes": 0.0,
        "gnbj_dl_latency_avg_ms": 0.0,
        "gnbj_sample_count": 0,
    }

    if not out:
        return gnbj

    # Accumulate per-sample lists
    acc = {k: [] for k in gnbj}

    dec = json.JSONDecoder()
    i = 0
    while i < len(out):
        try:
            obj, idx = dec.raw_decode(out, i)
            i = idx
        except json.JSONDecodeError:
            i += 1
            continue

        if not isinstance(obj, dict) or obj.get("type") != "metrics":
            continue

        # v22 JSON path: cell_list[0].cell_container.ue_list[0].ue_container
        u = {}
        for cell in obj.get("cell_list", []):
            cc = cell.get("cell_container", {})
            ue_list = cc.get("ue_list", [])
            if ue_list:
                u = ue_list[0].get("ue_container", {})
                break
        if not u:
            continue

        def fv(k, d=0.0):
            try: return float(u.get(k, d) or d)
            except: return d

        # Only accumulate MCS/SNR/PHR/BLER from samples with active traffic —
        # these fields are 0 when idle, which would skew the averages.
        brate_dl = fv("dl_bitrate")
        brate_ul = fv("ul_bitrate")
        has_traffic = brate_dl > 0 or brate_ul > 0

        acc["gnbj_dl_cqi_avg"].append(fv("dl_cqi"))   # CQI is valid even when idle
        if has_traffic:
            acc["gnbj_dl_mcs_avg"].append(fv("dl_mcs"))
            acc["gnbj_dl_mcs_max"].append(fv("dl_mcs"))
            acc["gnbj_ul_mcs_avg"].append(fv("ul_mcs"))
            acc["gnbj_ul_mcs_max"].append(fv("ul_mcs"))
            acc["gnbj_ul_snr_avg"].append(fv("ul_snr") or fv("ul_pusch_snr"))
            acc["gnbj_dl_bler_avg"].append(fv("dl_bler"))
            acc["gnbj_ul_bler_avg"].append(fv("ul_bler"))
            acc["gnbj_dl_bitrate_avg_mbps"].append(brate_dl / 1e6 if brate_dl > 1e3 else brate_dl)
            acc["gnbj_ul_bitrate_avg_mbps"].append(brate_ul / 1e6 if brate_ul > 1e3 else brate_ul)
            acc["gnbj_ul_phr_avg"].append(fv("ul_phr"))

        # RSSI/NI are valid any time UE is attached (PUCCH is always active)
        acc["gnbj_ul_pusch_rssi_avg"].append(fv("ul_pusch_rssi"))
        acc["gnbj_ul_pucch_rssi_avg"].append(fv("ul_pucch_rssi"))
        acc["gnbj_ul_pucch_ni_avg"].append(fv("ul_pucch_ni"))

        # Bearer bytes/latency come from bearer_container, not ue_container directly
        for bearer in u.get("bearer_list", []):
            bc = bearer.get("bearer_container", {})
            if bc.get("qci") == 9:
                dl_tb = float(bc.get("dl_total_bytes", 0) or 0)
                ul_tb = float(bc.get("ul_total_bytes", 0) or 0)
                dl_lat = float(bc.get("dl_latency", 0) or 0)
                if dl_tb > 0:
                    acc["gnbj_dl_total_bytes"].append(dl_tb)
                if ul_tb > 0:
                    acc["gnbj_ul_total_bytes"].append(ul_tb)
                if dl_lat > 0:
                    acc["gnbj_dl_latency_avg_ms"].append(dl_lat)
                break

    def sa(k): return round(sum(acc[k]) / len(acc[k]), 4) if acc[k] else 0.0
    def sm(k): return round(max(acc[k]), 4) if acc[k] else 0.0

    gnbj["gnbj_dl_cqi_avg"]          = sa("gnbj_dl_cqi_avg")
    gnbj["gnbj_dl_mcs_avg"]          = sa("gnbj_dl_mcs_avg")
    gnbj["gnbj_dl_mcs_max"]          = sm("gnbj_dl_mcs_max")
    gnbj["gnbj_ul_mcs_avg"]          = sa("gnbj_ul_mcs_avg")
    gnbj["gnbj_ul_mcs_max"]          = sm("gnbj_ul_mcs_max")
    gnbj["gnbj_ul_snr_avg"]          = sa("gnbj_ul_snr_avg")
    gnbj["gnbj_ul_pusch_rssi_avg"]   = sa("gnbj_ul_pusch_rssi_avg")
    gnbj["gnbj_ul_pucch_rssi_avg"]   = sa("gnbj_ul_pucch_rssi_avg")
    gnbj["gnbj_ul_pucch_ni_avg"]     = sa("gnbj_ul_pucch_ni_avg")
    gnbj["gnbj_dl_bler_avg"]         = sa("gnbj_dl_bler_avg")
    gnbj["gnbj_ul_bler_avg"]         = sa("gnbj_ul_bler_avg")
    gnbj["gnbj_dl_bitrate_avg_mbps"] = sa("gnbj_dl_bitrate_avg_mbps")
    gnbj["gnbj_ul_bitrate_avg_mbps"] = sa("gnbj_ul_bitrate_avg_mbps")
    gnbj["gnbj_ul_phr_avg"]          = sa("gnbj_ul_phr_avg")
    gnbj["gnbj_dl_total_bytes"]      = sa("gnbj_dl_total_bytes")
    gnbj["gnbj_ul_total_bytes"]      = sa("gnbj_ul_total_bytes")
    gnbj["gnbj_dl_latency_avg_ms"]   = sa("gnbj_dl_latency_avg_ms")
    gnbj["gnbj_sample_count"]        = len(acc["gnbj_dl_cqi_avg"])

    return gnbj


# ── CSV schema — 170-column full schema matching v22 sample ──────────────────
GNB_SNAP_FIELDS = [
    "gnb_ts_avg_mhz", "gnb_ts_busy_pct", "gnb_ts_bzy_mhz",
    "gnb_ts_ipc", "gnb_ts_irq", "gnb_ts_smi",
    "gnb_ts_c1_pct", "gnb_ts_c3_pct", "gnb_ts_c6_pct", "gnb_ts_c7_pct",
    "gnb_ts_core_tmp", "gnb_ts_pkg_tmp",
    "gnb_ts_pkg_pc2_pct", "gnb_ts_pkg_pc3_pct",
    "gnb_ts_pkg_watt", "gnb_ts_ram_watt",
    "gnb_ts_pkg_pct", "gnb_ts_ram_pct",
    "gnb_cpu_freq_avg_mhz", "gnb_cpu_freq_max_mhz",
    "gnb_load1", "gnb_load5", "gnb_load15",
    "gnb_rapl_pkg0_w", "gnb_cpu_user_pct", "gnb_cpu_sys_pct",
    "gnb_temp_c", "gnb_rapl_pkg1_w", "gnb_rapl_total_w",
    # legacy single-field alias
    "gnb_srsenb_cpu_pct",
]
GNB_RAN_FIELDS = [
    "ran_dl_mcs", "ran_ul_mcs",
    "ran_dl_brate_mbps", "ran_ul_brate_mbps",
    "ran_dl_prb", "ran_ul_prb",
    "ran_dl_tbs", "ran_ul_tbs",
    "ran_dl_bler", "ran_ul_bler",
    "ran_cqi", "ran_ri",
    "ran_pusch_sinr", "ran_phr",
    "ran_nof_ue", "ran_system_load",
    "ran_proc_rmem_kb", "ran_thread_count",
    "ran_gnb_cpu_avg_pct",
]
PHY_FIELDS = [
    "phy_dl_mcs_avg", "phy_dl_mcs_max",
    "phy_ul_mcs_avg", "phy_ul_mcs_max",
    "phy_dl_snr_avg", "phy_dl_snr_max",
    "phy_rsrp_avg", "phy_pathloss_avg",
    "phy_dl_brate_peak_mbps", "phy_dl_brate_avg_mbps",
    "phy_ul_brate_peak_mbps", "phy_ul_brate_avg_mbps",
    "phy_dl_bler_avg", "phy_ul_bler_avg",
    "phy_dl_turbo_avg", "phy_cfo_avg", "phy_ul_ta_avg",
    "phy_sample_count",
]
GNB_AGG_FIELDS = [
    "gnb_dl_brate_peak_mbps", "gnb_dl_brate_avg_mbps",
    "gnb_ul_brate_peak_mbps", "gnb_ul_brate_avg_mbps",
    "gnb_sys_load_avg", "gnb_cpu_avg_pct",
    "gnb_proc_rmem_kb_avg", "gnb_thread_count", "gnb_sample_count",
]
GNBJ_FIELDS = [
    "gnbj_dl_cqi_avg",
    "gnbj_dl_mcs_avg", "gnbj_dl_mcs_max",
    "gnbj_ul_mcs_avg", "gnbj_ul_mcs_max",
    "gnbj_ul_snr_avg",
    "gnbj_ul_pusch_rssi_avg", "gnbj_ul_pucch_rssi_avg", "gnbj_ul_pucch_ni_avg",
    "gnbj_dl_bler_avg", "gnbj_ul_bler_avg",
    "gnbj_dl_bitrate_avg_mbps", "gnbj_ul_bitrate_avg_mbps",
    "gnbj_ul_phr_avg",
    "gnbj_dl_total_bytes", "gnbj_ul_total_bytes",
    "gnbj_dl_latency_avg_ms",
    "gnbj_sample_count",
]

FIELDS = (
    ["ver", "ue_id", "n_attached", "attach_ms", "attach_ok", "ue_ip",
     "ping_avg_ms", "ping_min_ms", "ping_max_ms", "ping_jitter_ms", "ping_loss_pct"] +
    [f"dl_{r}m_mbps"     for r in DL_RATES] +
    [f"dl_{r}m_loss_pct" for r in DL_RATES] +
    [f"ul_{r}m_mbps"     for r in UL_RATES] +
    [f"ul_{r}m_loss_pct" for r in UL_RATES] +
    GNB_RAN_FIELDS +
    GNB_SNAP_FIELDS +
    PHY_FIELDS +
    GNB_AGG_FIELDS +
    GNBJ_FIELDS
)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\n{'#'*62}")
    print(f"  v22.10  50-UE Accumulation Experiment")
    print(f"  gnb1=pc802  uehost1=pc801  core=pc808")
    print(f"  Output: {OUT_FILE}")
    print(f"{'#'*62}\n")

    # ── Smart resume: load good rows, advance MEASURE_FROM ────────────────────
    measure_from = MEASURE_FROM
    existing_rows = []
    if OUT_FILE.exists() and OUT_FILE.stat().st_size > 0:
        with open(OUT_FILE, newline="") as fh:
            reader = csv.DictReader(fh)
            for r in reader:
                ue = int(r.get("ue_id", 0))
                try: ploss = float(r.get("ping_loss_pct", 100))
                except ValueError: ploss = 100.0
                attach_ok = r.get("attach_ok", "") == "OK"
                if attach_ok and ploss < 100.0:
                    existing_rows.append(r)
                    measure_from = max(measure_from, ue + 1)
    print(f"  Resuming: {len(existing_rows)} good rows already in CSV")
    print(f"  Will measure from UE {measure_from}")

    tmp = OUT_FILE.with_suffix(".tmp")
    with open(tmp, "w", newline="") as fh:
        w2 = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w2.writeheader()
        for r in existing_rows:
            for c in FIELDS: r.setdefault(c, "")
            w2.writerow(r)
    shutil.move(str(tmp), str(OUT_FILE))
    print(f"  CSV written: {len(existing_rows)} rows")

    # ── Kill all, restart EPC fresh ───────────────────────────────────────────
    print(f"\nKilling any leftover srsenb/srsue processes...")
    kill_all()

    print(f"\nRestarting EPC (resets SMF IP pool → avoids UPF spoofing drops)...")
    restart_epc()

    ssh(GNB_HOST, f"mkdir -p {GNB_LOG_DIR}", timeout=10)
    ssh(UE_HOST,  f"mkdir -p {UE_LOG_DIR}",  timeout=10)
 
    # ── GTP alias setup — MUST run before any srsenb starts ──────────────────
    print("\nSetting up unique GTP bind IPs (fixes DL GTP delivery to multi-process srsenb)...")
    setup_gtp_aliases(MAX_UE)

    # ── Start persistent iperf3 servers on core ───────────────────────────────
    print("Starting persistent iperf3 server loops on core (ports 5201-5250)...")
    ssh(CORE_HOST, "sudo pkill -9 iperf3 2>/dev/null; sleep 1; echo done", timeout=10)
    time.sleep(2)
    for n in range(1, MAX_UE + 1):
        port = 5200 + n
        loop = (f"while true; do "
                f"iperf3 -s -B {CORE_IP} -p {port} --one-off 2>/dev/null; "
                f"sleep 0.2; done")
        ssh_bg(CORE_HOST, loop)
    print("  Waiting for iperf3 ports to bind...")
    time.sleep(8)
    bound, _ = ssh(CORE_HOST,
                   "ss -tnlp | grep -cE ':52[0-9]{2} ' || echo 0", timeout=8)
    print(f"  iperf3 servers ready: {bound.strip()} ports listening")

    print("Pre-creating netns ue1..ue50 and cleaning NAS contexts...")
    ssh(UE_HOST,
        "for n in $(seq 1 50); do sudo ip netns add ue$n 2>/dev/null || true; done; "
        "rm -f /users/saish/.ctxt /root/.ctxt 2>/dev/null; "
        "for n in $(seq 1 50); do rm -f /tmp/ue_ctx/ue$n/.ctxt 2>/dev/null; done; "
        "mkdir -p /tmp/ue_ctx; "
        "echo \"netns_ready:$(ip netns list | wc -l)\"",
        timeout=40)

    n_attached = 0

    # ── Phase 1: fast-launch UEs 1 .. measure_from-1 ─────────────────────────
    if measure_from > 1:
        print(f"\n{'='*62}")
        print(f"  PHASE 1: Fast-launch UEs 1..{measure_from-1} (no measurements)")
        print(f"{'='*62}")
        for n in range(1, measure_from):
            print(f"  Fast-launch UE {n:2d}...", end=" ", flush=True)
            start_gnb_ue(n)
            time.sleep(1)
            if not wait_gnb_port(n, timeout=20):
                print("PORT_FAIL — skipping")
                continue
            start_srsue(n)
            time.sleep(2)

        print(f"\n  Waiting up to 90s for UEs 1-{measure_from-1} to attach...")
        time.sleep(90)

        # Inject default routes for all fast-launch UEs (primary ping fix)
        for n in range(1, measure_from):
            inject_default_route(n)

        attached_fast = []
        for n in range(1, measure_from):
            out, _ = ssh(UE_HOST,
                         f"sudo ip netns exec ue{n} ip -br a 2>/dev/null"
                         f" | grep tun | awk '{{print $3}}'",
                         timeout=12)
            if out.strip():
                attached_fast.append(n)
        n_attached = len(attached_fast)
        print(f"  Fast-launch done: {n_attached}/{measure_from-1} attached: {attached_fast}")
    else:
        print(f"\n  Starting fresh — no fast-launch phase needed (measure_from=1)")

    # ── Phase 2: measure UEs measure_from .. MAX_UE ───────────────────────────
    print(f"\n{'='*62}")
    print(f"  PHASE 2: Measurement loop UEs {measure_from}..{MAX_UE}")
    print(f"{'='*62}")

    def append_row(row_dict):
        desk_file = os.path.expanduser("~/Desktop/v22_10_ue_results_50.csv")
        with open(OUT_FILE, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writerow(row_dict)
            f.flush()
            os.fsync(f.fileno())
        try:
            shutil.copyfile(str(OUT_FILE), desk_file)
        except Exception:
            pass

    for n in range(measure_from, MAX_UE + 1):
        row = {"ver": "v22_10", "ue_id": n, "n_attached": n_attached}

        print(f"\n{'='*62}")
        print(f"  UE {n:2d}/50  [{time.strftime('%H:%M:%S')}]"
              f"  ({n_attached} UEs running)")
        print(f"{'='*62}")

        # 1. Start gNB
        print(f"  Starting srsenb {n}...")
        start_gnb_ue(n)
        port = 40000 + n * 10
        print(f"  Waiting for gNB port {port}...")
        if not wait_gnb_port(n):
            print(f"  gNB port not bound — skipping UE{n}")
            row.update({"attach_ok": "FAIL_GNB", "attach_ms": 0, "ue_ip": "",
                         "ping_avg_ms": 999, "ping_min_ms": 999,
                         "ping_max_ms": 999, "ping_jitter_ms": 999,
                         "ping_loss_pct": 100})
            for r in DL_RATES: row[f"dl_{r}m_mbps"] = 0.0; row[f"dl_{r}m_loss_pct"] = 100.0
            for r in UL_RATES: row[f"ul_{r}m_mbps"] = 0.0; row[f"ul_{r}m_loss_pct"] = 100.0
            row.update(snap_gnb_metrics())
            row.update({k: 0.0 for k in GNB_RAN_FIELDS})
            row.update({k: 0.0 for k in PHY_FIELDS})
            row.update({k: 0.0 for k in GNB_AGG_FIELDS})
            row.update({k: 0.0 for k in GNBJ_FIELDS})
            append_row(row)
            continue
        print(f"  gNB port {port} bound ✓")

        # 2. Start srsue
        print(f"  Starting srsue {n}...")
        start_srsue(n)
        time.sleep(2)

        # 3. Wait attach
        print(f"  Waiting for attach (timeout={ATTACH_TIMEOUT}s)...")
        ue_ip, attach_ms = wait_attach(n)

        if not ue_ip:
            print(f"  ATTACH FAILED — UE{n} ({attach_ms}ms)")
            row.update({"attach_ok": "FAIL", "attach_ms": attach_ms, "ue_ip": "",
                         "ping_avg_ms": 999, "ping_min_ms": 999,
                         "ping_max_ms": 999, "ping_jitter_ms": 999,
                         "ping_loss_pct": 100})
            for r in DL_RATES: row[f"dl_{r}m_mbps"] = 0.0; row[f"dl_{r}m_loss_pct"] = 100.0
            for r in UL_RATES: row[f"ul_{r}m_mbps"] = 0.0; row[f"ul_{r}m_loss_pct"] = 100.0
            row.update(snap_gnb_metrics())
            row.update({k: 0.0 for k in GNB_RAN_FIELDS})
            row.update({k: 0.0 for k in PHY_FIELDS})
            row.update({k: 0.0 for k in GNB_AGG_FIELDS})
            row.update({k: 0.0 for k in GNBJ_FIELDS})
            append_row(row)
            continue

        n_attached += 1
        row["n_attached"] = n_attached
        print(f"  Attached ✓  IP={ue_ip}  {attach_ms}ms  active={n_attached}")
        row.update({"attach_ok": "OK", "attach_ms": attach_ms, "ue_ip": ue_ip})

        # 4. Inject default route (primary fix for ping 100% loss)
        route_ok = inject_default_route(n)
        print(f"  default route: {'injected ✓' if route_ok else 'WARNING — could not inject'}")
        time.sleep(PING_SETTLE_S)

        # 5. Ping
        print("  Ping...")
        p_avg, p_min, p_max, p_jit, p_loss = run_ping(n)
        print(f"  ping avg={p_avg}ms loss={p_loss}%")

        if p_loss >= 100.0:
            healed, new_ip, new_ms = heal_upf(n)
            if healed:
                # Update attach info with fresh values from re-attach
                if new_ip:
                    ue_ip = new_ip
                    attach_ms = new_ms
                    row.update({"ue_ip": ue_ip, "attach_ms": attach_ms})
                # Ping already confirmed inside heal_upf; fetch fresh RTT
                p_avg, p_min, p_max, p_jit, p_loss = run_ping(n)
                print(f"  ping avg={p_avg}ms loss={p_loss}%")
            else:
                print(f"  [WARN] UPF heal failed UE{n} — writing row with loss=100, iperf skipped")

        row.update({"ping_avg_ms": p_avg, "ping_min_ms": p_min,
                     "ping_max_ms": p_max, "ping_jitter_ms": p_jit,
                     "ping_loss_pct": p_loss})

        # 5. DL iperf — 18 rates
        print("  DL iperf...")
        for r in DL_RATES:
            if p_loss >= 100.0:
                mbps, loss = 0.0, 100.0
                print(f"    DL {r:2d}M → skipped (ping loss=100%)")
            else:
                mbps, loss = run_iperf(n, r, "dl")
                print(f"    DL {r:2d}M → {mbps:.3f} Mbps  loss={loss}%")
                time.sleep(1)
            row[f"dl_{r}m_mbps"] = mbps
            row[f"dl_{r}m_loss_pct"] = loss

        # 6. UL iperf — 18 rates
        # RAN params + agg + gnbj snapped DURING the 2nd UL rate via bg thread
        print("  UL iperf...")
        ran_bg = {}; agg_bg = {}; gnbj_bg = {}; phy_bg = {}
        ul_snap_done = False
        for idx_r, r in enumerate(UL_RATES):
            if p_loss >= 100.0:
                mbps, loss = 0.0, 100.0
                print(f"    UL {r:2d}M → skipped (ping loss=100%)")
            else:
                mbps, loss = run_iperf(n, r, "ul")
                print(f"    UL {r:2d}M → {mbps:.3f} Mbps  loss={loss}%")
                time.sleep(1)
                # Snap all log-derived metrics during active UL traffic
                if idx_r == 1 and not ul_snap_done:
                    def _snap_all():
                        ran_bg.update(snap_gnb_ran(n))
                        agg_bg.update(snap_gnb_agg(n))
                        gnbj_bg.update(snap_gnbj(n))
                        phy_bg.update(snap_ue_phy(n))
                    t = threading.Thread(target=_snap_all, daemon=True)
                    t.start()
                    ul_snap_done = True
            row[f"ul_{r}m_mbps"] = mbps
            row[f"ul_{r}m_loss_pct"] = loss
        if ul_snap_done:
            t.join(timeout=30)

        # 7. gNB RAN params
        print("  gNB RAN params...")
        ran = ran_bg if ran_bg else snap_gnb_ran(n)
        print(f"  dl_mcs={ran['ran_dl_mcs']}  ul_mcs={ran['ran_ul_mcs']}"
              f"  cqi={ran['ran_cqi']}  sinr={ran['ran_pusch_sinr']}dB")
        row.update(ran)

        # 8. gNB power / turbostat snapshot
        print("  gNB power snapshot (turbostat)...")
        snap = snap_gnb_metrics()
        print(f"  pkg={snap['gnb_ts_pkg_watt']}W(ts)"
              f"  rapl={snap['gnb_rapl_total_w']}W"
              f"  busy={snap['gnb_ts_busy_pct']}%"
              f"  load={snap['gnb_load1']}/{snap['gnb_load5']}"
              f"  CoreTmp={snap['gnb_ts_core_tmp']}°C")
        row.update(snap)

        # 9. gNB metrics CSV aggregates
        agg = agg_bg if agg_bg else snap_gnb_agg(n)
        print(f"  gnb_agg: dl_peak={agg['gnb_dl_brate_peak_mbps']}Mbps"
              f"  n_samples={agg['gnb_sample_count']}")
        row.update(agg)

        # 10. gNB report JSON averages
        gnbj = gnbj_bg if gnbj_bg else snap_gnbj(n)
        print(f"  gnbj: cqi={gnbj['gnbj_dl_cqi_avg']}"
              f"  dl_mcs={gnbj['gnbj_dl_mcs_avg']}"
              f"  n_samples={gnbj['gnbj_sample_count']}")
        row.update(gnbj)

        # 11. UE PHY metrics
        phy = phy_bg if phy_bg else snap_ue_phy(n)
        print(f"  phy: dl_mcs_avg={phy['phy_dl_mcs_avg']}"
              f"  dl_snr_avg={phy['phy_dl_snr_avg']}"
              f"  rsrp={phy['phy_rsrp_avg']}")
        row.update(phy)

        append_row(row)
        print(f"  ✓ UE{n} saved (synced to Desktop).")

    # ── Done ──────────────────────────────────────────────────────────────────
    print(f"\n{'#'*62}")
    print(f"  v22.10 experiment complete!")
    with open(OUT_FILE, newline="") as fh:
        total = sum(1 for _ in csv.DictReader(fh))
    print(f"  Rows in CSV: {total} / {MAX_UE}")
    print(f"  Output: {OUT_FILE}")
    print(f"{'#'*62}")


if __name__ == "__main__":
    main()
