#!/usr/bin/env python3
"""
run_v23_50ue.py — v23.11 50-UE accumulation experiment on POWDER.
================================================================
Node mapping:
  core    = pc808   10.10.1.1   Open5GS EPC (MME+SGW+UPF)
  gnb1    = pc802   10.10.1.2   srsenb v23.11 — one process per UE
  uehost1 = pc801   10.10.1.4   srsue  v23.11 — one process per UE

Config paths (installed on nodes):
  gNB:  /etc/srsenb_v23/enb_ue{n}.conf
        ZMQ tx=tcp://*:4{nn}0  rx=tcp://10.10.1.4:4{nn}1
  UE:   /etc/srsue_v23/ue{n}.conf
        netns=ue{n}  tun=tun_srsue{n}

Port formula: UE n → base_port = 40000 + n*10
  gNB tx = base_port     (DL to UE)
  gNB rx = base_port + 1 (UL from UE)

Output: results/ver_eval/v23_11/ue_results_50.csv
  — separate file from v22_10, same full schema
  — 18 DL rates (1-10 + 15,20,25,30,35,40,45,50 Mbps)
  — 18 UL rates (same)
  — turbostat: Avg_MHz, Busy%, Bzy_MHz, IPC, IRQ, SMI,
               C1/C3/C6/C7 residency, CoreTmp, PkgTmp,
               Pkg%pc2/pc3, PkgWatt, RAMWatt, PKG_%, RAM_%
  — RAPL pkg0+pkg1 W
  — RAN: dl_mcs, ul_mcs, dl/ul_brate, prb, tbs, bler, cqi,
          ri, pusch_sinr, phr, nof_ue, system_load, cpu%
  — system: loadavg, cpu_freq_mhz, srsenb_cpu_pct

Experiment design (accumulation — all previous UEs stay running):
  PHASE 1 (1..MEASURE_FROM-1): fast-launch gNB+UE, wait attach, no measurements
  PHASE 2 (MEASURE_FROM..50):  full ping + 18-rate DL/UL iperf + snapshots per UE

Built-in fixes:
  - heal_upf(): auto-detects UPF spoofing DROP (100% ping loss) and restarts
    only upfd/sgwud/sgwcd/smfd without killing any gNB/srsue process
  - iperf early-exit: skips all 36 iperf calls when ping loss stays at 100%
  - smart resume: discards bad rows (100% loss) on restart; advances MEASURE_FROM
    to last good UE + 1 automatically
"""

import subprocess, time, csv, os, re, json, pathlib, hashlib, shutil, signal, threading

# ── Node config ───────────────────────────────────────────────────────────────
KEY       = os.path.expanduser("~/.ssh/id_ed25519")
GNB_HOST  = "saish@pc802.emulab.net"
UE_HOST   = "saish@pc801.emulab.net"
CORE_HOST = "saish@pc808.emulab.net"

# v23.11 binary — the single srsRAN build on both nodes is Version 23.4.0
# (release_23_11 tag; version string reports 23.04.0 — documented mismatch)
SRSENB_BIN   = "/opt/srsRAN_src/build/srsenb/src/srsenb"
SRSUE_BIN    = "/opt/srsRAN_src/build/srsue/src/srsue"
# Re-use the v22 configs — format is identical for v23 (s1c_bind_port supported,
# rb_config key is the same). Avoids having to install a separate config set.
ENB_CONF_DIR = "/etc/srsenb_v22"
UE_CONF_DIR  = "/etc/srsue_v22"
UE_LOG_DIR   = "/tmp/ue_logs_v23"
GNB_LOG_DIR  = "/tmp/gnb_logs_v23"

ATTACH_TIMEOUT = 75      # seconds — generous for slow attaches at high UE counts
PING_COUNT     = 20
DL_RATES = list(range(1, 11)) + list(range(15, 51, 5))   # 18 rates: 1-10,15,20,...,50
UL_RATES = list(range(1, 11)) + list(range(15, 51, 5))   # 18 rates: same
IPERF_DUR      = 10
MAX_UE         = 50
MEASURE_FROM   = 16      # fast-launch UEs 1-15 (no measurements), collect from UE 16
CORE_IP        = "10.45.0.1"

# ── Output — SEPARATE from v22_10 ────────────────────────────────────────────
OUT_DIR  = pathlib.Path("results/ver_eval/v23_11")
OUT_FILE = OUT_DIR / "ue_results_50.csv"      # new file, same full schema

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
    statuses = [l.strip() for l in out.splitlines()
                if l.strip() in ("active", "inactive", "failed")]
    all_active = all(s == "active" for s in statuses)
    print(f"  EPC restart {'OK' if all_active else 'WARN'}: {statuses}")
    return all_active


# ── gNB: start one srsenb for UE n ───────────────────────────────────────────
def start_gnb_ue(n):
    conf     = f"{ENB_CONF_DIR}/enb_ue{n}.conf"
    log      = f"{GNB_LOG_DIR}/enb_ue{n}.log"
    met_csv  = f"{GNB_LOG_DIR}/enb_ue{n}_metrics.csv"
    rpt_json = f"{GNB_LOG_DIR}/enb_ue{n}_report.json"
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


# ── UE: start srsue n ────────────────────────────────────────────────────────
def start_srsue(n):
    conf    = f"{UE_CONF_DIR}/ue{n}.conf"
    log     = f"{UE_LOG_DIR}/ue{n}.log"
    ue_csv  = f"{UE_LOG_DIR}/ue{n}_metrics.csv"
    ctx_dir = f"/tmp/ue_ctx_v23/ue{n}"
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
    """Poll for tun_srsue{n} in netns ue{n}. Returns (ip, elapsed_ms)."""
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
    cmd = (f"sudo ip netns exec ue{n} ping -c {c}"
           f" -i 0.3 -W 2 {CORE_IP}")
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
    Fix 100% ping loss caused by Open5GS UPF 'Source IP-4 Spoofing' DROP.

    Root cause: UPF has a stale PFCP session (leftover TEID from a previous run)
    whose stored UE-IP collides with the new UE's UL GTP packets → UPF drops
    all UL traffic as spoofing. The stale entry survives EPC restarts when
    ZMQ gNBs reconnect and re-register the same TEID.

    Strategy:
      1. SIGTERM srsue{n} → NAS Detach → MME removes the live bearer.
      2. Kill gNB slot {n}.
      3. Restart ONLY open5gs-upfd — clears all stale PFCP sessions WITHOUT
         resetting the SMF IP pool (SMF/MME stay up → UEs 1..n-1 unaffected).
      4. Wait 8s for UPF to come back and UEs 1..n-1 to re-register sessions.
      5. Restart gNB slot {n} + re-attach srsue{n}.
      6. Confirm ping.

    Returns (recovered: bool, new_ue_ip: str, new_attach_ms: int).
    """
    print(f"  [HEAL] UPF spoofing DROP on UE{ue_n} — restarting UPF only...")

    # Step 1: graceful srsue detach (NAS Detach removes MME bearer)
    print(f"  [HEAL] Step 1/4: NAS Detach srsue{ue_n}...")
    ssh(UE_HOST,
        f"sudo pkill -TERM -f 'srsue.*ue{ue_n}\\.conf' 2>/dev/null; sleep 3; "
        f"sudo pkill -9   -f 'srsue.*ue{ue_n}\\.conf' 2>/dev/null; sleep 1; echo done",
        timeout=15)

    # Step 2: kill gNB slot
    print(f"  [HEAL] Step 2/4: killing srsenb{ue_n}...")
    ssh(GNB_HOST,
        f"sudo pkill -9 -f 'srsenb.*enb_ue{ue_n}\\.conf' 2>/dev/null; sleep 1; echo done",
        timeout=12)

    # Step 3: restart ONLY upfd — clears stale PFCP, SMF pool untouched
    print(f"  [HEAL] Step 3/4: restarting open5gs-upfd only...")
    out, _ = ssh(CORE_HOST,
        "sudo systemctl restart open5gs-upfd && sleep 5\n"
        "echo upfd=$(systemctl is-active open5gs-upfd)",
        timeout=20)
    print(f"  [HEAL] UPF: {out.strip()}")
    # Give existing UE sessions (1..n-1) time to re-register with fresh UPF
    print(f"  [HEAL] Waiting 15s for UEs 1-{ue_n-1} to re-register PFCP sessions...")
    time.sleep(15)

    # Step 4: restart gNB slot + re-attach
    print(f"  [HEAL] Step 4/4: restarting gNB slot {ue_n} + re-attaching...")
    start_gnb_ue(ue_n)
    if not wait_gnb_port(ue_n, timeout=25):
        print(f"  [HEAL] gNB port not bound — heal failed")
        return False, "", 0

    ctx_dir = f"/tmp/ue_ctx_v23/ue{ue_n}"
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

    p_avg, _, _, _, p_loss = run_ping(ue_n, count=5)
    print(f"  [HEAL] Post-heal ping: avg={p_avg}ms loss={p_loss}%")
    if p_loss < 50.0:
        print(f"  [HEAL] ✓ RECOVERED")
        return True, new_ip, new_ms
    print(f"  [HEAL] Still failing — heal failed")
    return False, new_ip, new_ms


def ensure_iperf_server(n):
    """
    Ensure a persistent looping iperf3 server is running on CORE_HOST for
    port 5200+n.  iperf3 exits after each single client by default; the
    while-true wrapper restarts it immediately so consecutive iperf calls
    from the same UE don't hit a dead port.
    Only spawns a new loop when the port is not already listening.
    """
    port = 5200 + n
    out, _ = ssh(CORE_HOST,
                 f"ss -tnlp 2>/dev/null | grep -c ':{port} ' || echo 0",
                 timeout=8)
    if out.strip() == "1":
        return   # already listening
    loop = (f"while true; do "
            f"iperf3 -s -B {CORE_IP} -p {port} --one-off 2>/dev/null; "
            f"sleep 0.2; done")
    ssh_bg(CORE_HOST, loop)
    time.sleep(1)   # wait for bind


def run_iperf(n, rate_mbps, direction="dl"):
    """
    Run iperf3 from UE n netns. Ensures the per-UE persistent server is alive
    before connecting (restarts it if the previous client consumed it).
    Returns (mbps, loss_pct).
    """
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
TS_OUT=$(sed -n '3p' $TSFILE)
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
        "gnb_cpu_freq_mhz": 0.0,  # legacy alias
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


# ── gNB RAN params snapshot ───────────────────────────────────────────────────
def snap_gnb_ran(n):
    """
    Parse enb_ue{n}_metrics.csv (semicolon-separated, v23.11 schema) and
    enb_ue{n}_report.json (nested cell_list structure) for RAN params.

    v23.11 metrics CSV columns (semicolon-separated):
      time;nof_ue;dl_brate;ul_brate;proc_rmem;proc_rmem_kB;proc_vmem_kB;
      sys_mem;system_load;thread_count;cpu_0..cpu_31

    v23.11 report JSON path for per-UE params:
      cell_list[0].cell_container.ue_list[0].ue_container
      fields: dl_cqi, dl_mcs, ul_mcs, dl_bler, ul_bler, ul_snr, ul_phr,
              dl_bitrate, ul_bitrate
    """
    script = f"""
MET="{GNB_LOG_DIR}/enb_ue{n}_metrics.csv"
RPT="{GNB_LOG_DIR}/enb_ue{n}_report.json"
if [ -f "$MET" ]; then
  echo "METRICS_HDR:$(head -1 $MET)"
  echo "METRICS_LAST:$(tail -10 $MET | grep -v '^$' | tail -1)"
fi
if [ -f "$RPT" ]; then
  tail -c 200000 "$RPT"
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
            # v23.11 CSV uses semicolon separator
            hdr = line[12:].split(";")
        elif line.startswith("METRICS_LAST:"):
            last = line[13:].split(";")
        else:
            json_lines.append(line)

    # ── metrics CSV (system-level fields only — per-UE RAN in JSON) ───────────
    if hdr and last:
        row_map = dict(zip(hdr, last + [""] * max(0, len(hdr) - len(last))))
        def g(k):
            try: return float(row_map.get(k, 0) or 0)
            except: return 0.0
        ran["ran_nof_ue"]          = int(g("nof_ue"))
        ran["ran_dl_brate_mbps"]   = round(g("dl_brate") / 1e6, 4) if g("dl_brate") > 1 else g("dl_brate")
        ran["ran_ul_brate_mbps"]   = round(g("ul_brate") / 1e6, 4) if g("ul_brate") > 1 else g("ul_brate")
        ran["ran_system_load"]     = g("system_load")
        ran["ran_proc_rmem_kb"]    = int(g("proc_rmem_kB") or g("proc_rmem_kb") or g("rmem_kb"))
        ran["ran_thread_count"]    = int(g("thread_count") or g("nof_threads"))
        # system_load also doubles as gnb cpu avg
        ran["ran_gnb_cpu_avg_pct"] = g("system_load")

    # ── report JSON — parse last complete object ──────────────────────────────
    json_text = "\n".join(json_lines)
    dec = json.JSONDecoder()
    last_j = None
    i = 0
    while i < len(json_text):
        try:
            obj, idx = dec.raw_decode(json_text, i)
            if isinstance(obj, dict) and obj.get("type") == "metrics":
                last_j = obj
            i = idx
        except json.JSONDecodeError:
            i += 1

    if last_j:
        # v23.11 JSON path: cell_list[0].cell_container.ue_list[0].ue_container
        try:
            uc = (last_j["cell_list"][0]["cell_container"]
                       ["ue_list"][0]["ue_container"])
            def uf(k, d=0.0):
                try: return float(uc.get(k, d) or d)
                except: return d
            ran["ran_cqi"]            = uf("dl_cqi")
            ran["ran_dl_mcs"]         = uf("dl_mcs")
            ran["ran_ul_mcs"]         = uf("ul_mcs")
            ran["ran_dl_bler"]        = uf("dl_bler")
            ran["ran_ul_bler"]        = uf("ul_bler")
            ran["ran_pusch_sinr"]     = uf("ul_snr")
            ran["ran_phr"]            = int(uf("ul_phr"))
            # dl_bitrate / ul_bitrate are in bps in report JSON
            brate_dl = uf("dl_bitrate")
            brate_ul = uf("ul_bitrate")
            if brate_dl > 0:
                ran["ran_dl_brate_mbps"] = round(brate_dl / 1e6, 4)
            if brate_ul > 0:
                ran["ran_ul_brate_mbps"] = round(brate_ul / 1e6, 4)
        except (KeyError, IndexError, TypeError):
            pass   # no UE attached yet — leave zeros

    return ran


# ── PHY metrics from srsue metrics CSV ───────────────────────────────────────
def snap_ue_phy(n):
    """
    Parse the last 30 rows of /tmp/ue_logs_v23/ue{n}_metrics.csv on pc801.

    v23 srsue metrics CSV is SEMICOLON-separated. Real field names:
      rsrp, pl, cfo, dl_mcs, dl_snr, dl_turbo, dl_brate, dl_bler,
      ul_ta, ul_mcs, ul_brate, ul_bler
    (NOT the dl_-prefixed names used in v22 comma-sep format)
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
            # v23 srsue CSV is semicolon-separated
            hdr = line[8:].split(";")
        elif line.startswith("PHY_ROW:"):
            rows.append(line[8:].split(";"))

    if not hdr or not rows:
        return phy

    def col_vals(key):
        idx = None
        for i, h in enumerate(hdr):
            if h.strip() == key:
                idx = i; break
        if idx is None:
            return []
        vals = []
        for r in rows:
            if idx < len(r):
                try: vals.append(float(r[idx]))
                except (ValueError, TypeError): pass
        return vals

    def safe_avg(vals): return round(sum(vals) / len(vals), 4) if vals else 0.0
    def safe_max(vals): return round(max(vals), 4) if vals else 0.0

    dl_mcs = col_vals("dl_mcs"); phy["phy_dl_mcs_avg"] = safe_avg(dl_mcs); phy["phy_dl_mcs_max"] = safe_max(dl_mcs)
    ul_mcs = col_vals("ul_mcs"); phy["phy_ul_mcs_avg"] = safe_avg(ul_mcs); phy["phy_ul_mcs_max"] = safe_max(ul_mcs)
    dl_snr = col_vals("dl_snr"); phy["phy_dl_snr_avg"] = safe_avg(dl_snr); phy["phy_dl_snr_max"] = safe_max(dl_snr)
    # v23 real field names: rsrp (not dl_rsrp), pl (not dl_pathloss)
    phy["phy_rsrp_avg"]      = safe_avg(col_vals("rsrp"))
    phy["phy_pathloss_avg"]  = safe_avg(col_vals("pl"))

    dl_b = [v / 1e6 for v in col_vals("dl_brate") if v > 0]
    phy["phy_dl_brate_peak_mbps"] = safe_max(dl_b)
    phy["phy_dl_brate_avg_mbps"]  = safe_avg(dl_b)

    ul_b = [v / 1e6 for v in col_vals("ul_brate") if v > 0]
    phy["phy_ul_brate_peak_mbps"] = safe_max(ul_b)
    phy["phy_ul_brate_avg_mbps"]  = safe_avg(ul_b)

    phy["phy_dl_bler_avg"]   = safe_avg(col_vals("dl_bler"))
    phy["phy_ul_bler_avg"]   = safe_avg(col_vals("ul_bler"))
    # v23 real field names: dl_turbo (not dl_turbo_iters), cfo (not dl_cfo_hz), ul_ta (not ul_ta_us)
    phy["phy_dl_turbo_avg"]  = safe_avg(col_vals("dl_turbo"))
    phy["phy_cfo_avg"]       = safe_avg(col_vals("cfo"))
    phy["phy_ul_ta_avg"]     = safe_avg(col_vals("ul_ta"))
    phy["phy_sample_count"]  = len(rows)

    return phy


# ── gNB metrics CSV window aggregates (v23 — semicolon-sep) ──────────────────
def snap_gnb_agg(n):
    """
    Parse the last 30 rows of enb_ue{n}_metrics.csv (semicolon-sep, v23) and
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
            # v23 metrics CSV is semicolon-separated
            hdr = line[9:].split(";")
        elif line.startswith("GAGG_ROW:"):
            rows.append(line[9:].split(";"))

    if not hdr or not rows:
        return agg

    def col_vals(key):
        idx = None
        for i, h in enumerate(hdr):
            if h.strip() == key:
                idx = i; break
        if idx is None:
            return []
        vals = []
        for r in rows:
            if idx < len(r):
                try: vals.append(float(r[idx]))
                except (ValueError, TypeError): pass
        return vals

    def safe_avg(vals): return round(sum(vals) / len(vals), 4) if vals else 0.0
    def safe_max(vals): return round(max(vals), 4) if vals else 0.0

    dl_b = [v / 1e6 for v in col_vals("dl_brate") if v > 0]
    agg["gnb_dl_brate_peak_mbps"] = safe_max(dl_b)
    agg["gnb_dl_brate_avg_mbps"]  = safe_avg(dl_b)

    ul_b = [v / 1e6 for v in col_vals("ul_brate") if v > 0]
    agg["gnb_ul_brate_peak_mbps"] = safe_max(ul_b)
    agg["gnb_ul_brate_avg_mbps"]  = safe_avg(ul_b)

    agg["gnb_sys_load_avg"] = safe_avg(col_vals("system_load"))

    cpu = col_vals("system_load")  # v23 uses system_load as cpu proxy
    agg["gnb_cpu_avg_pct"] = safe_avg(cpu)

    rmem = col_vals("proc_rmem_kB")
    if not rmem: rmem = col_vals("proc_rmem_kb")
    agg["gnb_proc_rmem_kb_avg"] = safe_avg(rmem)

    thr = col_vals("thread_count")
    agg["gnb_thread_count"] = int(safe_avg(thr))
    agg["gnb_sample_count"] = len(rows)

    return agg


# ── gNB report JSON window aggregates (gnbj_* — v23 path) ───────────────────
def snap_gnbj(n):
    """
    Parse ALL JSON objects in enb_ue{n}_report.json and average per-UE fields.
    v23 JSON path: cell_list[0].cell_container.ue_list[0].ue_container
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

        # v23 path: cell_list[0].cell_container.ue_list[0].ue_container
        try:
            uc = (obj["cell_list"][0]["cell_container"]
                      ["ue_list"][0]["ue_container"])
        except (KeyError, IndexError, TypeError):
            continue

        def fv(k, d=0.0):
            try: return float(uc.get(k, d) or d)
            except: return d

        acc["gnbj_dl_cqi_avg"].append(fv("dl_cqi"))
        acc["gnbj_dl_mcs_avg"].append(fv("dl_mcs"))
        acc["gnbj_dl_mcs_max"].append(fv("dl_mcs"))
        acc["gnbj_ul_mcs_avg"].append(fv("ul_mcs"))
        acc["gnbj_ul_mcs_max"].append(fv("ul_mcs"))
        acc["gnbj_ul_snr_avg"].append(fv("ul_snr"))
        acc["gnbj_ul_pusch_rssi_avg"].append(fv("ul_pusch_rssi"))
        acc["gnbj_ul_pucch_rssi_avg"].append(fv("ul_pucch_rssi"))
        acc["gnbj_ul_pucch_ni_avg"].append(fv("ul_pucch_ni"))
        acc["gnbj_dl_bler_avg"].append(fv("dl_bler"))
        acc["gnbj_ul_bler_avg"].append(fv("ul_bler"))
        b_dl = fv("dl_bitrate")
        b_ul = fv("ul_bitrate")
        acc["gnbj_dl_bitrate_avg_mbps"].append(b_dl / 1e6 if b_dl > 1 else b_dl)
        acc["gnbj_ul_bitrate_avg_mbps"].append(b_ul / 1e6 if b_ul > 1 else b_ul)
        acc["gnbj_ul_phr_avg"].append(fv("ul_phr"))
        # dl_total_bytes / ul_total_bytes / dl_latency live inside
        # bearer_list[0].bearer_container, NOT directly on ue_container
        try:
            bc = uc["bearer_list"][0]["bearer_container"]
            acc["gnbj_dl_total_bytes"].append(float(bc.get("dl_total_bytes", 0) or 0))
            acc["gnbj_ul_total_bytes"].append(float(bc.get("ul_total_bytes", 0) or 0))
            acc["gnbj_dl_latency_avg_ms"].append(float(bc.get("dl_latency", 0) or 0) * 1000)
        except (KeyError, IndexError, TypeError):
            acc["gnbj_dl_total_bytes"].append(0.0)
            acc["gnbj_ul_total_bytes"].append(0.0)
            acc["gnbj_dl_latency_avg_ms"].append(0.0)

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


# ── CSV schema — 170-column full schema (IDENTICAL to v22_10) ─────────────────
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
    print(f"  v23.11  50-UE Accumulation Experiment")
    print(f"  gnb1=pc802  uehost1=pc801  core=pc808")
    print(f"  Output: {OUT_FILE}")
    print(f"{'#'*62}\n")

    # ── Load existing rows; auto-detect last good UE (smart resume) ───────────
    # "Good" = attach_ok=OK AND ping_loss_pct < 100.
    # Bad rows (UPF spoofing run) are discarded so the UE is re-measured.
    measure_from = MEASURE_FROM   # local copy — may advance during resume scan
    existing_rows = []
    if OUT_FILE.exists() and OUT_FILE.stat().st_size > 0:
        with open(OUT_FILE, newline="") as fh:
            reader = csv.DictReader(fh)
            for r in reader:
                ue = int(r.get("ue_id", 0))
                try:
                    ploss = float(r.get("ping_loss_pct", 100))
                except ValueError:
                    ploss = 100.0
                attach_ok = r.get("attach_ok", "") == "OK"
                if attach_ok and ploss < 100.0:
                    existing_rows.append(r)
                    measure_from = max(measure_from, ue + 1)
    print(f"  Resuming: {len(existing_rows)} good rows already in CSV")
    print(f"  Will measure from UE {measure_from}")

    # Rewrite CSV — good rows only, correct schema
    tmp = OUT_FILE.with_suffix(".tmp")
    with open(tmp, "w", newline="") as fh:
        w2 = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w2.writeheader()
        for r in existing_rows:
            for c in FIELDS:
                r.setdefault(c, "")
            w2.writerow(r)
    shutil.move(str(tmp), str(OUT_FILE))
    print(f"  CSV written: {len(existing_rows)} rows")

    # ── Kill all, restart EPC fresh, pre-create netns ─────────────────────────
    print(f"\nKilling any leftover srsenb/srsue processes...")
    kill_all()

    print(f"\nRestarting EPC (resets SMF IP pool → avoids UPF spoofing drops)...")
    restart_epc()

    ssh(GNB_HOST, f"mkdir -p {GNB_LOG_DIR}", timeout=10)
    ssh(UE_HOST,  f"mkdir -p {UE_LOG_DIR}",  timeout=10)

    # ── (Re)start persistent iperf3 server loops on core for all 50 UE ports ──
    # Kill any stale single-shot servers, then spin up while-true loops so
    # each port stays alive across all 18 consecutive iperf calls per UE.
    print("Starting persistent iperf3 server loops on core (ports 5201-5250)...")
    ssh(CORE_HOST, "sudo pkill -9 iperf3 2>/dev/null; sleep 1; echo done", timeout=10)
    time.sleep(2)
    for n in range(1, MAX_UE + 1):
        port = 5200 + n
        loop = (f"while true; do "
                f"iperf3 -s -B {CORE_IP} -p {port} --one-off 2>/dev/null; "
                f"sleep 0.2; done")
        ssh_bg(CORE_HOST, loop)
    # Wait for all 50 ports to bind
    print("  Waiting for iperf3 ports to bind...")
    time.sleep(8)
    bound, _ = ssh(CORE_HOST,
                   "ss -tnlp | grep -cE ':52[0-9]{2} ' || echo 0", timeout=8)
    print(f"  iperf3 servers ready: {bound.strip()} ports listening")

    print("Pre-creating netns ue1..ue50 and cleaning NAS contexts...")
    ssh(UE_HOST,
        "for n in $(seq 1 50); do sudo ip netns add ue$n 2>/dev/null || true; done; "
        "rm -f /users/saish/.ctxt /root/.ctxt 2>/dev/null; "
        "for n in $(seq 1 50); do rm -f /tmp/ue_ctx_v23/ue$n/.ctxt 2>/dev/null; done; "
        "mkdir -p /tmp/ue_ctx_v23; "
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

    with open(OUT_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")

        for n in range(measure_from, MAX_UE + 1):
            row = {"ver": "v23_11", "ue_id": n, "n_attached": n_attached}

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
                w.writerow(row); f.flush(); os.fsync(f.fileno())
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
                w.writerow(row); f.flush(); os.fsync(f.fileno())
                continue

            n_attached += 1
            row["n_attached"] = n_attached
            print(f"  Attached ✓  IP={ue_ip}  {attach_ms}ms  active={n_attached}")
            row.update({"attach_ok": "OK", "attach_ms": attach_ms, "ue_ip": ue_ip})
            time.sleep(2)

            # 4. Ping
            print("  Ping...")
            p_avg, p_min, p_max, p_jit, p_loss = run_ping(n)
            print(f"  ping avg={p_avg}ms loss={p_loss}%")

            # ── Auto-recover UPF spoofing DROP ────────────────────────────────
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

            # 5. DL iperf — 18 rates (skip all if ping still 100% loss)
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

            # 6. UL iperf — 18 rates (skip if ping still 100% loss)
            # All log-derived metrics snapped DURING the 2nd UL rate via bg thread
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

            # 7. gNB RAN params — use live snapshot if collected, else snap now
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

            w.writerow(row); f.flush(); os.fsync(f.fileno())
            print(f"  ✓ UE{n} saved.")

    # ── Done ──────────────────────────────────────────────────────────────────
    print(f"\n{'#'*62}")
    print(f"  v23.11 experiment complete!")
    with open(OUT_FILE, newline="") as fh:
        total = sum(1 for _ in csv.DictReader(fh))
    print(f"  Rows in CSV: {total} / {MAX_UE}")
    print(f"  Output: {OUT_FILE}")
    print(f"{'#'*62}")


if __name__ == "__main__":
    main()
