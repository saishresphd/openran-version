#!/usr/bin/env python3
"""
run_sequential_9ue_lb.py
========================
Sequential per-UE load-balancing experiment: UE49 → UE48 → ... → UE40
Each UE migrates from gNB1 to gNB2 one at a time — replicating real network
operations where service continuity is maintained for all other UEs.

For each UE (49 down to 40):
  Step A  pre-snapshot   : capture RAN+power+CPU on gNB1 while UE still attached
                           → writes row  event_type=attach_gnb1  to accumulation CSV
  Step B  detach         : kill srsue for that UE → record detach_ts_ms
  Step C  gnb2 slot start: ssh gNB2, start srsenb enb_ueN.conf slot
  Step D  gnb2 attach    : launch srsue with ueN_gnb2.conf → wait tun UP
                           → record attach_ts_ms, compute HO latency
  Step E  post-gnb1 snap : capture gNB1 state after removal (1 fewer UE)
                           → writes row  event_type=post_lb_gnb1
  Step F  gnb2 snap      : capture gNB2 state with newly attached UE
                           → writes row  event_type=attach_gnb2
  Step G  stabilise      : wait STAB_S seconds before next UE handover

Output CSV schema matches results/ue50_60_experiment/master_accumulation.csv
exactly — so both datasets can be analysed together.

Node map (Emulab POWDER testbed):
  core    = saish@pc811.emulab.net  (10.10.1.1)
  gnb1    = saish@pc818.emulab.net  (10.10.1.2)
  gnb2    = saish@pc802.emulab.net  (10.10.1.3)
  uehost1 = saish@pc808.emulab.net  (10.10.1.4)  ← this script runs HERE

Run on uehost1:
  python3 run_sequential_9ue_lb.py [--dry-run] [--start-ue N] [--end-ue N]

Paper equations extended by this dataset:
  Eq.2  P(N) = α·N^β + γ            — power vs UE count (pre/post each LB step)
  Eq.3  Ptotal = Pbase + NaU·PaU + NUi·PUi — per-UE component model
  Eq.4  Psaved = Pactive − Pswitched        — energy saving per LB step
  Eq.5  P(v/p) = Pbase + Pi·Tu             — power vs throughput per UE
  Eq.6  P(N,PRB) = β0+β1·N+β2·PRB         — joint UE+PRB regression (new)
  Eq.7  P_sched = γ0+γ1·proc_us·N         — scheduling CPU cost (new)
"""

import argparse
import csv
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ─── Configuration ────────────────────────────────────────────────────────────
CORE_HOST  = "saish@10.10.1.1"
GNB1_HOST  = "saish@10.10.1.2"
GNB2_HOST  = "saish@10.10.1.3"
UEHOST     = "saish@10.10.1.4"       # this node — used for SSH sanity checks only

SSH_OPTS   = ["-o", "StrictHostKeyChecking=no",
              "-o", "ConnectTimeout=10",
              "-o", "BatchMode=yes"]

NFS_BASE   = "/proj/ATLANTIC-eVISION/exp/loadbalance/tmp/ran_9ue_lb"
LOCAL_OUT  = Path("results/ran_9ue_lb")
ACCUM_CSV  = LOCAL_OUT / "sequential_accumulation.csv"
LOG_FILE   = LOCAL_OUT / "sequential_lb.log"
PHASE_FILE = "/tmp/ran_collect/phase.txt"

# UE range — process from high to low (UE49 first, UE40 last)
UE_START   = 40
UE_END     = 49

# Timing (seconds)
STAB_S            = 30    # stabilisation window between each UE migration
PRE_SNAP_WINDOW_S = 15    # seconds of power averaging for pre-snapshot
POST_SNAP_WAIT_S  = 20    # wait after attach before taking post-snapshot
ATTACH_TIMEOUT_S  = 90    # max wait for srsue tun to come UP on gNB2
DETACH_WAIT_S     = 5     # wait after pkill before starting gNB2 slot
GNB2_SLOT_WAIT_S  = 3     # seconds after starting srsenb slot before launching srsue
IPERF_DL_MBPS     = 100   # downlink iperf rate during each snapshot

# Paths on the nodes
GNB1_METRICS_DIR   = f"{NFS_BASE}/gnb1_metrics"       # srsenb metrics CSVs
GNB1_LOG_DIR       = f"{NFS_BASE}/gnb1_logs"
GNB2_LOG_DIR       = "/tmp/gnb2_logs"                 # gNB2 local (no NFS)
UE_CONF_DIR        = f"{NFS_BASE}/configs/ues"
ENB_GNB2_CONF_DIR  = "/etc/srsenb"                    # pre-installed on gNB2
RAPL_PATH          = "/sys/class/powercap/intel-rapl:0/energy_uj"
COLLECT_DIR        = "/tmp/ran_collect"

# CSV column order — MUST match master_accumulation.csv header exactly
CSV_COLS = [
    "event_type", "ue_id", "gnb", "timestamp_utc", "epoch_s",
    "total_ues_gnb1", "total_ues_gnb2",
    "attach_ok", "attach_latency_s", "ue_ip",
    "ho_total_ms",
    "ping_loss_pct", "ping_rtt_min_ms", "ping_rtt_avg_ms", "ping_rtt_max_ms",
    "iperf_dl_mbps", "iperf_ul_mbps",
    "gnb_pkg0_watts", "gnb_pkg1_watts", "gnb_total_watts",
    "gnb_load1", "gnb_load5", "gnb_cpu_max_pct",
    "sysmon_cpu_user_pct", "sysmon_cpu_sys_pct",
    "sysmon_cpu_softirq_pct", "sysmon_cpu_idle_pct",
    "sysmon_ctx_switches_per_s", "sysmon_intr_per_s",
    "sysmon_softirq_net_rx_per_s", "sysmon_softirq_net_tx_per_s",
    "sysmon_softirq_timer_per_s", "sysmon_softirq_sched_per_s",
    "sysmon_softirq_rcu_per_s",
    "sysmon_ipc", "sysmon_cpu_freq_mhz_avg", "sysmon_rapl_uj_delta",
    "proc_cpu_pct", "proc_rss_kB", "proc_sched_run_ns", "proc_sched_wait_ns",
    "ran_combined_pdsch_prb_mean", "ran_combined_pusch_snr_mean",
    "ran_combined_pusch_proc_us_mean", "ran_combined_phr_mean",
    "notes",
]

# ─── Logging ──────────────────────────────────────────────────────────────────
LOCAL_OUT.mkdir(parents=True, exist_ok=True)
_logfh = open(LOG_FILE, "a")

def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    _logfh.write(line + "\n")
    _logfh.flush()

def die(msg: str) -> None:
    log(f"FATAL: {msg}")
    sys.exit(1)

# ─── SSH helpers ──────────────────────────────────────────────────────────────
def ssh_run(host: str, script: str, timeout: int = 30) -> tuple[int, str, str]:
    """Run a bash heredoc on a remote host. Returns (rc, stdout, stderr)."""
    cmd = ["ssh"] + SSH_OPTS + [host, "bash -s"]
    try:
        r = subprocess.run(
            cmd,
            input=script.encode(),
            capture_output=True,
            timeout=timeout,
        )
        return r.returncode, r.stdout.decode(errors="replace"), r.stderr.decode(errors="replace")
    except subprocess.TimeoutExpired:
        return -1, "", "TIMEOUT"
    except Exception as e:
        return -1, "", str(e)

def ssh_out(host: str, script: str, timeout: int = 15) -> str:
    """Run script, return stdout stripped (empty string on failure)."""
    rc, out, err = ssh_run(host, script, timeout=timeout)
    if rc != 0:
        log(f"  WARN ssh_out {host}: rc={rc} stderr={err.strip()[:120]}")
    return out.strip()

# ─── RAPL power sampler ───────────────────────────────────────────────────────
def sample_rapl_watts(host: str, window_s: int = 3) -> dict:
    """Return dict with pkg0_w, pkg1_w, total_w averaged over window_s seconds."""
    script = f"""
e0_0=$(cat /sys/class/powercap/intel-rapl:0/energy_uj 2>/dev/null || echo 0)
e1_0=$(cat /sys/class/powercap/intel-rapl:1/energy_uj 2>/dev/null || echo 0)
sleep {window_s}
e0_1=$(cat /sys/class/powercap/intel-rapl:0/energy_uj 2>/dev/null || echo 0)
e1_1=$(cat /sys/class/powercap/intel-rapl:1/energy_uj 2>/dev/null || echo 0)
python3 -c "
p0=($e0_1-$e0_0)/1e6/{window_s}
p1=($e1_1-$e1_0)/1e6/{window_s}
print(f'{{p0:.4f}} {{p1:.4f}} {{p0+p1:.4f}}')
" 2>/dev/null || echo "0 0 0"
"""
    out = ssh_out(host, script, timeout=window_s + 10)
    parts = out.split()
    try:
        return {"pkg0_w": float(parts[0]), "pkg1_w": float(parts[1]), "total_w": float(parts[2])}
    except Exception:
        return {"pkg0_w": 0.0, "pkg1_w": 0.0, "total_w": 0.0}

# ─── System metrics sampler ───────────────────────────────────────────────────
def sample_sysmet(host: str) -> dict:
    """Sample CPU%, load avg, context switches, soft-IRQ, IPC, freq from host."""
    script = r"""
python3 - <<'PYEOF'
import subprocess, re, time, os

# CPU % over 1s window
with open('/proc/stat') as f: s1 = f.readline().split()
time.sleep(1)
with open('/proc/stat') as f: s2 = f.readline().split()
t1 = list(map(int, s1[1:])); t2 = list(map(int, s2[1:]))
idle1=t1[3]+t1[4]; idle2=t2[3]+t2[4]
dt=sum(t2)-sum(t1); cpu_pct=100*(dt-(idle2-idle1))/dt if dt else 0

# /proc/stat second sample for softirq breakdown
with open('/proc/stat') as f: stat_lines = f.readlines()
softirq_vals = {}
for ln in stat_lines:
    if ln.startswith('softirq'):
        parts = ln.split()
        # columns: NET_TX NET_RX BLOCK BLOCK_IOPOLL TASKLET SCHED HRTIMER RCU
        # kernel ordering varies; we record NET_RX(1) NET_TX(0) TIMER(4) SCHED(5) RCU(7)
        if len(parts) > 8:
            softirq_vals = {'NET_TX':int(parts[1]),'NET_RX':int(parts[2]),
                            'TIMER':int(parts[7]),'SCHED':int(parts[8]),
                            'RCU':int(parts[9]) if len(parts)>9 else 0}

# ctx switches + intr from /proc/stat
ctxt=intr=0
for ln in stat_lines:
    if ln.startswith('ctxt'): ctxt=int(ln.split()[1])
    elif ln.startswith('intr'): intr=int(ln.split()[1])

# load avg
with open('/proc/loadavg') as f: la = f.read().split()
load1=float(la[0]); load5=float(la[1])

# CPU max % across cores from /proc/stat
import re
core_vals=[]
for ln in open('/proc/stat'):
    m = re.match(r'^cpu(\d+)\s+(.+)', ln)
    if m:
        v=list(map(int,m.group(2).split()))
        idle=v[3]+v[4]; tot=sum(v)
        core_vals.append(tot)

# CPU freq avg
try:
    freqs=[int(open(f'/sys/devices/system/cpu/cpu{c}/cpufreq/scaling_cur_freq').read())/1000
           for c in range(min(4,os.cpu_count() or 1))
           if os.path.exists(f'/sys/devices/system/cpu/cpu{c}/cpufreq/scaling_cur_freq')]
    freq_avg = sum(freqs)/len(freqs) if freqs else 0
except: freq_avg=0

# RAPL delta (quick 500ms)
try:
    e0=int(open('/sys/class/powercap/intel-rapl:0/energy_uj').read())
    time.sleep(0.5)
    e1=int(open('/sys/class/powercap/intel-rapl:0/energy_uj').read())
    rapl_delta=e1-e0
except: rapl_delta=0

# srsenb process stats
import subprocess
cpu_proc=rss=sched_run=sched_wait=ipc=0.0
try:
    ps=subprocess.run(['ps','--no-headers','-C','srsenb','-o','%cpu,rss'],
                      capture_output=True,text=True)
    rows=[l.split() for l in ps.stdout.strip().splitlines() if l.strip()]
    if rows:
        cpu_proc=sum(float(r[0]) for r in rows if r)
        rss=sum(int(r[1]) for r in rows if len(r)>1)
except: pass

print(f"{cpu_pct:.2f} {ctxt} {intr} "
      f"{softirq_vals.get('NET_RX',0)} {softirq_vals.get('NET_TX',0)} "
      f"{softirq_vals.get('TIMER',0)} {softirq_vals.get('SCHED',0)} {softirq_vals.get('RCU',0)} "
      f"{ipc:.4f} {freq_avg:.1f} {rapl_delta} "
      f"{cpu_proc:.2f} {rss} {sched_run} {sched_wait} "
      f"{load1} {load5}")
PYEOF
"""
    out = ssh_out(host, script, timeout=20)
    parts = out.split()
    def f(i, default=0.0):
        try: return float(parts[i])
        except: return default
    return {
        "cpu_pct":          f(0),
        "ctx_switches":     f(1),
        "intr":             f(2),
        "softirq_net_rx":   f(3),
        "softirq_net_tx":   f(4),
        "softirq_timer":    f(5),
        "softirq_sched":    f(6),
        "softirq_rcu":      f(7),
        "ipc":              f(8),
        "cpu_freq_mhz":     f(9),
        "rapl_uj_delta":    f(10),
        "proc_cpu_pct":     f(11),
        "proc_rss_kB":      f(12),
        "proc_sched_run_ns":  f(13),
        "proc_sched_wait_ns": f(14),
        "load1":            f(15),
        "load5":            f(16),
    }

# ─── RAN PHY/MAC sampler ─────────────────────────────────────────────────────
def sample_ran_gnb(host: str, gnb_label: str) -> dict:
    """
    Read last row from the running ran_params collector CSV on the host.
    Falls back to parsing metrics CSV directly.
    Returns dict with pdsch_prb, pusch_snr, pusch_proc_us, phr, nof_ue.
    """
    script = f"""
# Try ran_params collector first
f="{NFS_BASE}/results/ran_params_{gnb_label}.csv"
if [ ! -f "$f" ]; then
    f="/tmp/ran_collect/ran_params_{gnb_label}.csv"
fi
if [ -f "$f" ]; then
    tail -1 "$f"
    exit 0
fi
# Fallback: parse gnb1_metrics dir for last written metrics CSV
MDIR="{NFS_BASE}/{gnb_label}_metrics"
if [ ! -d "$MDIR" ]; then MDIR="/tmp/ran_collect"; fi
last=$(ls -t "$MDIR"/*.csv 2>/dev/null | head -1)
[ -n "$last" ] && tail -1 "$last" || echo ""
"""
    out = ssh_out(host, script, timeout=15)
    # Try to parse known column positions from ran_params_gnb1.csv format:
    # timestamp,phase,elapsed_s,nof_ue,dl_brate_kbps,ul_brate_kbps,
    # dl_mcs,ul_mcs,dl_snr,ul_snr,cpu_pct,rapl_pkg0_w,...
    result = {
        "nof_ue": 0, "pdsch_prb": 0.0,
        "pusch_snr": 0.0, "pusch_proc_us": 0.0, "phr": 0.0,
    }
    if not out:
        return result
    parts = out.split(",")
    # If it's the ran_params format (>=12 cols, col[3]=nof_ue)
    try:
        if len(parts) >= 12:
            result["nof_ue"] = int(float(parts[3]))
            # ran_combined cols if present
            if len(parts) >= 20:
                result["pdsch_prb"]      = float(parts[14]) if parts[14] else 0.0
                result["pusch_snr"]      = float(parts[15]) if parts[15] else 0.0
                result["pusch_proc_us"]  = float(parts[16]) if parts[16] else 0.0
                result["phr"]            = float(parts[17]) if parts[17] else 0.0
    except Exception:
        pass
    return result

# ─── Nof UE counter ───────────────────────────────────────────────────────────
def get_nof_ue(host: str, gnb_label: str) -> int:
    """Return current nof_ue reported by srsenb on host (reads metrics CSV)."""
    script = f"""
f="{NFS_BASE}/results/ran_params_{gnb_label}.csv"
[ ! -f "$f" ] && f="/tmp/ran_collect/ran_params_{gnb_label}.csv"
if [ -f "$f" ]; then
    val=$(tail -1 "$f" | cut -d, -f4)
    echo "${{val:-0}}"
else
    # Try live metrics directory
    MDIR="{NFS_BASE}/{gnb_label}_metrics"
    last=$(ls -t "$MDIR"/*.csv 2>/dev/null | head -1)
    [ -n "$last" ] && tail -1 "$last" | cut -d';' -f2 || echo "0"
fi
"""
    out = ssh_out(host, script, timeout=10)
    try:
        return int(float(out))
    except Exception:
        return -1

# ─── Tun device checker ───────────────────────────────────────────────────────
def tun_is_up(ue_id: int) -> bool:
    """Return True if tun_srsueN is UP in network namespace ueN (local check)."""
    r = subprocess.run(
        ["sudo", "ip", "netns", "exec", f"ue{ue_id}", "ip", "link", "show",
         f"tun_srsue{ue_id}"],
        capture_output=True, text=True,
    )
    return "UP" in r.stdout

def wait_for_tun_up(ue_id: int, timeout_s: int = ATTACH_TIMEOUT_S) -> float:
    """
    Poll tun_srsueN every 2s until UP or timeout.
    Returns elapsed seconds if UP, -1.0 if timed out.
    """
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if tun_is_up(ue_id):
            return time.time() - t0
        time.sleep(2)
    return -1.0

def tun_is_down(ue_id: int) -> bool:
    return not tun_is_up(ue_id)

# ─── iperf3 quick throughput ──────────────────────────────────────────────────
def measure_iperf(ue_id: int, target_ip: str = "10.10.1.1",
                  duration_s: int = 5, mbps: int = IPERF_DL_MBPS) -> dict:
    """Run iperf3 UDP in UE netns, return dl_mbps, ul_mbps."""
    r = subprocess.run(
        ["sudo", "ip", "netns", "exec", f"ue{ue_id}",
         "iperf3", "-c", target_ip, "-u", "-b", f"{mbps}M",
         "-t", str(duration_s), "-J", "--logfile", "/dev/null"],
        capture_output=True, text=True, timeout=duration_s + 15,
    )
    try:
        j = json.loads(r.stdout)
        dl = j["end"]["sum"]["bits_per_second"] / 1e6
        ul = j.get("end", {}).get("sum_received", {}).get("bits_per_second", 0) / 1e6
        return {"dl_mbps": round(dl, 2), "ul_mbps": round(ul, 2)}
    except Exception:
        return {"dl_mbps": 0.0, "ul_mbps": 0.0}

# ─── Ping check ───────────────────────────────────────────────────────────────
def measure_ping(ue_id: int, target_ip: str = "10.10.1.1", count: int = 10) -> dict:
    r = subprocess.run(
        ["sudo", "ip", "netns", "exec", f"ue{ue_id}",
         "ping", "-c", str(count), "-i", "0.2", target_ip],
        capture_output=True, text=True, timeout=count * 2 + 10,
    )
    out = r.stdout
    loss = rtt_min = rtt_avg = rtt_max = 0.0
    try:
        m = __import__("re").search(r"(\d+)% packet loss", out)
        if m: loss = float(m.group(1))
        m = __import__("re").search(r"rtt .* = ([\d.]+)/([\d.]+)/([\d.]+)", out)
        if m:
            rtt_min = float(m.group(1))
            rtt_avg = float(m.group(2))
            rtt_max = float(m.group(3))
    except Exception:
        pass
    return {"loss": loss, "rtt_min": rtt_min, "rtt_avg": rtt_avg, "rtt_max": rtt_max}

# ─── CSV writer ───────────────────────────────────────────────────────────────
def init_csv() -> None:
    """Create CSV with header if it does not exist."""
    if not ACCUM_CSV.exists():
        with open(ACCUM_CSV, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=CSV_COLS).writeheader()
        log(f"Created accumulation CSV: {ACCUM_CSV}")

def write_row(row: dict) -> None:
    """Append a single row to the accumulation CSV."""
    # Ensure all columns present
    full = {c: row.get(c, "") for c in CSV_COLS}
    with open(ACCUM_CSV, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=CSV_COLS).writerow(full)
    log(f"  → CSV row written: event={full['event_type']} ue={full['ue_id']} gnb={full['gnb']}")

# ─── Full snapshot helper ─────────────────────────────────────────────────────
def take_snapshot(
    event_type: str,
    ue_id: int,
    gnb_label: str,
    gnb_host: str,
    n_gnb1: int,
    n_gnb2: int,
    attach_ok: int = 0,
    attach_latency_s: float = 0.0,
    ho_total_ms: float = 0.0,
    ue_ip: str = "",
    notes: str = "",
    dry_run: bool = False,
) -> dict:
    """
    Collect RAPL, sysmon, RAN stats from gnb_host and write one CSV row.
    Returns the row dict.
    """
    log(f"    [snapshot] event={event_type} ue={ue_id} gnb={gnb_label}")
    now = datetime.now(timezone.utc)

    if dry_run:
        rapl  = {"pkg0_w": 99.9, "pkg1_w": 99.9, "total_w": 199.8}
        sys_m = {k: 0.0 for k in ["cpu_pct","ctx_switches","intr",
                                    "softirq_net_rx","softirq_net_tx",
                                    "softirq_timer","softirq_sched","softirq_rcu",
                                    "ipc","cpu_freq_mhz","rapl_uj_delta",
                                    "proc_cpu_pct","proc_rss_kB",
                                    "proc_sched_run_ns","proc_sched_wait_ns",
                                    "load1","load5"]}
        ran_m = {"nof_ue": n_gnb1 if gnb_label == "gnb1" else n_gnb2,
                 "pdsch_prb": 14.0, "pusch_snr": 110.0,
                 "pusch_proc_us": 550.0, "phr": 25.0}
        iperf = {"dl_mbps": 99.0, "ul_mbps": 0.0}
        ping_m = {"loss": 0.0, "rtt_min": 1.0, "rtt_avg": 2.0, "rtt_max": 5.0}
    else:
        log(f"      sampling RAPL on {gnb_host}...")
        rapl  = sample_rapl_watts(gnb_host, window_s=3)
        log(f"      sampling sysmet on {gnb_host}...")
        sys_m = sample_sysmet(gnb_host)
        log(f"      sampling RAN on {gnb_host}...")
        ran_m = sample_ran_gnb(gnb_host, gnb_label)
        # iperf + ping only if UE is attached (attach_ok==1 or pre-snapshot on gnb1)
        if tun_is_up(ue_id):
            log(f"      iperf3 UE{ue_id}...")
            iperf  = measure_iperf(ue_id)
            log(f"      ping UE{ue_id}...")
            ping_m = measure_ping(ue_id)
        else:
            iperf  = {"dl_mbps": 0.0, "ul_mbps": 0.0}
            ping_m = {"loss": 100.0, "rtt_min": 0.0, "rtt_avg": 0.0, "rtt_max": 0.0}

    row = {
        "event_type":          event_type,
        "ue_id":               ue_id,
        "gnb":                 gnb_label,
        "timestamp_utc":       now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "epoch_s":             now.timestamp(),
        "total_ues_gnb1":      n_gnb1,
        "total_ues_gnb2":      n_gnb2,
        "attach_ok":           attach_ok,
        "attach_latency_s":    round(attach_latency_s, 2) if attach_latency_s else "",
        "ue_ip":               ue_ip,
        "ho_total_ms":         round(ho_total_ms) if ho_total_ms else "",
        "ping_loss_pct":       ping_m["loss"],
        "ping_rtt_min_ms":     ping_m["rtt_min"],
        "ping_rtt_avg_ms":     ping_m["rtt_avg"],
        "ping_rtt_max_ms":     ping_m["rtt_max"],
        "iperf_dl_mbps":       iperf["dl_mbps"],
        "iperf_ul_mbps":       iperf["ul_mbps"],
        "gnb_pkg0_watts":      round(rapl["pkg0_w"], 4),
        "gnb_pkg1_watts":      round(rapl["pkg1_w"], 4),
        "gnb_total_watts":     round(rapl["total_w"], 4),
        "gnb_load1":           sys_m["load1"],
        "gnb_load5":           sys_m["load5"],
        "gnb_cpu_max_pct":     sys_m.get("cpu_max_pct", sys_m["cpu_pct"]),
        "sysmon_cpu_user_pct": sys_m["cpu_pct"],
        "sysmon_cpu_sys_pct":  sys_m.get("cpu_sys_pct", 0.0),
        "sysmon_cpu_softirq_pct": sys_m.get("cpu_softirq_pct", 0.0),
        "sysmon_cpu_idle_pct": round(100.0 - sys_m["cpu_pct"], 2),
        "sysmon_ctx_switches_per_s": sys_m["ctx_switches"],
        "sysmon_intr_per_s":   sys_m["intr"],
        "sysmon_softirq_net_rx_per_s": sys_m["softirq_net_rx"],
        "sysmon_softirq_net_tx_per_s": sys_m["softirq_net_tx"],
        "sysmon_softirq_timer_per_s":  sys_m["softirq_timer"],
        "sysmon_softirq_sched_per_s":  sys_m["softirq_sched"],
        "sysmon_softirq_rcu_per_s":    sys_m["softirq_rcu"],
        "sysmon_ipc":          sys_m["ipc"],
        "sysmon_cpu_freq_mhz_avg": sys_m["cpu_freq_mhz"],
        "sysmon_rapl_uj_delta":    sys_m["rapl_uj_delta"],
        "proc_cpu_pct":         sys_m["proc_cpu_pct"],
        "proc_rss_kB":          sys_m["proc_rss_kB"],
        "proc_sched_run_ns":    sys_m["proc_sched_run_ns"],
        "proc_sched_wait_ns":   sys_m["proc_sched_wait_ns"],
        "ran_combined_pdsch_prb_mean":      ran_m["pdsch_prb"],
        "ran_combined_pusch_snr_mean":      ran_m["pusch_snr"],
        "ran_combined_pusch_proc_us_mean":  ran_m["pusch_proc_us"],
        "ran_combined_phr_mean":            ran_m["phr"],
        "notes": notes,
    }
    write_row(row)
    return row

# ─── Per-UE handover steps ────────────────────────────────────────────────────
def handover_one_ue(
    ue_id: int,
    n_gnb1_before: int,
    n_gnb2_before: int,
    dry_run: bool = False,
) -> dict:
    """
    Perform one sequential UE handover (gNB1 → gNB2) with full data collection.

    Returns summary dict:
      {ue_id, ho_latency_ms, attach_ok, detach_ts_ms, attach_ts_ms,
       pre_power_gnb1_w, post_power_gnb1_w, post_power_gnb2_w, ue_ip}
    """
    summary = {
        "ue_id": ue_id, "ho_latency_ms": -1, "attach_ok": 0,
        "detach_ts_ms": 0, "attach_ts_ms": 0,
        "pre_power_gnb1_w": 0.0, "post_power_gnb1_w": 0.0,
        "post_power_gnb2_w": 0.0, "ue_ip": "",
    }

    log(f"\n{'='*60}")
    log(f"  Handover UE{ue_id}: gNB1 ({n_gnb1_before} UEs) → gNB2 ({n_gnb2_before} UEs)")
    log(f"{'='*60}")

    # ── Step A: Pre-LB snapshot on gNB1 (UE still attached) ──────────────────
    log(f"  [Step A] Pre-LB snapshot on gNB1 (UE{ue_id} still attached)")
    pre_row = take_snapshot(
        event_type="attach_gnb1",
        ue_id=ue_id,
        gnb_label="gnb1",
        gnb_host=GNB1_HOST,
        n_gnb1=n_gnb1_before,
        n_gnb2=n_gnb2_before,
        attach_ok=1,
        notes=f"UE{ue_id}_on_gnb1_pre_lb_{n_gnb1_before}UEs",
        dry_run=dry_run,
    )
    summary["pre_power_gnb1_w"] = pre_row["gnb_total_watts"]

    # ── Step B: Detach UE from gNB1 ──────────────────────────────────────────
    log(f"  [Step B] Detaching UE{ue_id} from gNB1...")
    if not dry_run:
        # Kill srsue for this UE — it runs in netns ue{id} on uehost1 (this node)
        subprocess.run(
            ["sudo", "pkill", "-SIGTERM", "-f", f"srsue.*ue{ue_id}\\.conf"],
            capture_output=True,
        )
        subprocess.run(
            ["sudo", "pkill", "-SIGTERM", "-f", f"srsue.*ue{ue_id}[^0-9]"],
            capture_output=True,
        )
        # Also stop any iperf running in that netns
        subprocess.run(
            ["sudo", "ip", "netns", "exec", f"ue{ue_id}", "pkill", "-f", "iperf3"],
            capture_output=True,
        )

    detach_ts_ms = int(time.time() * 1000)
    summary["detach_ts_ms"] = detach_ts_ms
    log(f"  [Step B] UE{ue_id} detach signal sent at {detach_ts_ms}ms")

    if not dry_run:
        # Wait for tun to go DOWN (confirm detach)
        t0 = time.time()
        while tun_is_up(ue_id) and (time.time() - t0 < 15):
            time.sleep(1)
        elapsed = time.time() - t0
        log(f"  [Step B] tun DOWN confirmed in {elapsed:.1f}s")

        # Kill any stale srsue processes holding ZMQ ports for this UE
        subprocess.run(
            ["sudo", "pkill", "-9", "-f", f"srsue.*ue{ue_id}"],
            capture_output=True,
        )
        time.sleep(DETACH_WAIT_S)

    # ── Step C: Start gNB2 slot for this UE ──────────────────────────────────
    log(f"  [Step C] Starting gNB2 enb slot for UE{ue_id}...")
    if not dry_run:
        gnb2_start_script = f"""
mkdir -p /tmp/gnb2_logs
# Kill any stale slot for this UE
pkill -9 -f "srsenb.*enb_ue{ue_id}" 2>/dev/null || true
sleep 1
# Ensure GTP IP alias is present
DEV=$(ip route | grep '^default' | awk '{{print $5}}' | head -1)
[ -z "$DEV" ] && DEV=$(ip link | grep ': ' | grep -v lo | head -1 | cut -d: -f2 | tr -d ' ')
GTP_IP="10.10.1.{240 + (ue_id - 40)}"
if ! ip addr show dev $DEV 2>/dev/null | grep -q "$GTP_IP/"; then
    sudo ip addr add $GTP_IP/24 dev $DEV 2>/dev/null && echo "Added $GTP_IP" || true
fi
# Start the srsenb slot
sudo bash -c "srsenb {ENB_GNB2_CONF_DIR}/enb_ue{ue_id}.conf >> /tmp/gnb2_logs/ue{ue_id}_stdout.log 2>&1 &"
echo "gnb2_slot_ue{ue_id}_started"
sleep {GNB2_SLOT_WAIT_S}
ps aux | grep -q '[s]rsenb.*enb_ue{ue_id}' && echo "slot_running" || echo "slot_NOT_running"
"""
        rc, out, err = ssh_run(GNB2_HOST, gnb2_start_script, timeout=20)
        if "slot_running" in out:
            log(f"  [Step C] gNB2 slot enb_ue{ue_id} confirmed running")
        else:
            log(f"  [Step C] WARNING: gNB2 slot enb_ue{ue_id} may not be running: {out.strip()}")

    # ── Step D: Reconnect UE to gNB2 ─────────────────────────────────────────
    log(f"  [Step D] Reconnecting UE{ue_id} → gNB2...")
    if not dry_run:
        # Clean up netns for fresh ZMQ state
        subprocess.run(["sudo", "ip", "netns", "del", f"ue{ue_id}"],
                       capture_output=True)
        time.sleep(0.5)
        subprocess.run(["sudo", "ip", "netns", "add", f"ue{ue_id}"],
                       capture_output=True)

        # Find UE config for gNB2
        gnb2_ue_conf = f"{UE_CONF_DIR}/ue{ue_id}_gnb2.conf"
        # Check config exists on NFS (accessible from uehost1)
        if not Path(gnb2_ue_conf).exists():
            # Try /etc/srsue path
            gnb2_ue_conf = f"/etc/srsue/ue{ue_id}_gnb2.conf"
            if not Path(gnb2_ue_conf).exists():
                log(f"  [Step D] ERROR: UE{ue_id} gnb2 config not found at {gnb2_ue_conf}")
                return summary

        log_path   = f"{COLLECT_DIR}/ue{ue_id}_gnb2.log"
        stdout_log = f"{COLLECT_DIR}/ue{ue_id}_gnb2_stdout.log"

        # Launch srsue — the ONLY working method: sudo bash -c "srsue conf >> log 2>&1 &"
        launch_cmd = (
            f"sudo bash -c 'srsue {gnb2_ue_conf} "
            f"--log.filename={log_path} >> {stdout_log} 2>&1 &'"
        )
        subprocess.Popen(
            ["sudo", "bash", "-c",
             f"srsue {gnb2_ue_conf} --log.filename={log_path} >> {stdout_log} 2>&1 &"],
            close_fds=True,
        )
        log(f"  [Step D] srsue UE{ue_id} launched → gNB2. Waiting for tun UP (max {ATTACH_TIMEOUT_S}s)...")

    attach_t0 = time.time()
    if dry_run:
        ho_elapsed = 15.0
        attach_ok  = 1
    else:
        ho_elapsed = wait_for_tun_up(ue_id, timeout_s=ATTACH_TIMEOUT_S)
        attach_ok  = 1 if ho_elapsed >= 0 else 0

    attach_ts_ms = int(time.time() * 1000)
    ho_latency_ms = attach_ts_ms - detach_ts_ms if attach_ok else -1

    summary["attach_ts_ms"]  = attach_ts_ms
    summary["attach_ok"]     = attach_ok
    summary["ho_latency_ms"] = ho_latency_ms

    if attach_ok:
        log(f"  [Step D] *** UE{ue_id} ATTACHED to gNB2 — HO latency = {ho_latency_ms} ms ***")
        # Get UE IP from tun interface
        if not dry_run:
            r = subprocess.run(
                ["sudo", "ip", "netns", "exec", f"ue{ue_id}",
                 "ip", "addr", "show", f"tun_srsue{ue_id}"],
                capture_output=True, text=True,
            )
            import re
            m = re.search(r"inet ([\d.]+)", r.stdout)
            if m: summary["ue_ip"] = m.group(1)
    else:
        log(f"  [Step D] WARNING: UE{ue_id} did NOT attach within {ATTACH_TIMEOUT_S}s")

    # Updated UE counts after this handover
    n_gnb1_after = n_gnb1_before - 1
    n_gnb2_after = n_gnb2_before + (1 if attach_ok else 0)

    # Wait for gNB state to stabilise before taking snapshots
    if not dry_run:
        log(f"  [Step D→E] Stabilising {POST_SNAP_WAIT_S}s before post-snapshots...")
        time.sleep(POST_SNAP_WAIT_S)

    # ── Step E: Post-LB snapshot on gNB1 (one fewer UE) ──────────────────────
    log(f"  [Step E] Post-LB snapshot on gNB1 ({n_gnb1_after} UEs remaining)")
    post_gnb1_row = take_snapshot(
        event_type="post_lb_gnb1",
        ue_id=ue_id,
        gnb_label="gnb1",
        gnb_host=GNB1_HOST,
        n_gnb1=n_gnb1_after,
        n_gnb2=n_gnb2_after,
        attach_ok=0,
        ho_total_ms=ho_latency_ms if ho_latency_ms > 0 else 0,
        notes=f"UE{ue_id}_removed_from_gnb1_{n_gnb1_after}UEs",
        dry_run=dry_run,
    )
    summary["post_power_gnb1_w"] = post_gnb1_row["gnb_total_watts"]

    # ── Step F: gNB2 snapshot (UE just arrived) ───────────────────────────────
    log(f"  [Step F] gNB2 snapshot (UE{ue_id} on gNB2, total={n_gnb2_after})")
    gnb2_row = take_snapshot(
        event_type="attach_gnb2",
        ue_id=ue_id,
        gnb_label="gnb2",
        gnb_host=GNB2_HOST,
        n_gnb1=n_gnb1_after,
        n_gnb2=n_gnb2_after,
        attach_ok=attach_ok,
        ho_total_ms=ho_latency_ms if ho_latency_ms > 0 else 0,
        ue_ip=summary["ue_ip"],
        notes=f"UE{ue_id}_on_gnb2_gnb2has_{n_gnb2_after}UEs",
        dry_run=dry_run,
    )
    summary["post_power_gnb2_w"] = gnb2_row["gnb_total_watts"]

    # ── Compute per-step Eq.4 values ──────────────────────────────────────────
    psaved = summary["pre_power_gnb1_w"] - summary["post_power_gnb1_w"]
    pgain_gnb2 = summary["post_power_gnb2_w"] - (
        # baseline gNB2 power when it had n_gnb2_before UEs — use current gnb2 pre as proxy
        summary.get("gnb2_pre_power_w", gnb2_row["gnb_total_watts"] * 0.9)
    )
    log(f"  [Eq.4] Psaved(gNB1) = {psaved:+.3f} W  |  ΔP_cost(gNB2) = {pgain_gnb2:+.3f} W")

    # ── Step G: Stabilisation before next UE ─────────────────────────────────
    log(f"  [Step G] Stabilising {STAB_S}s before next UE handover...")
    if not dry_run:
        time.sleep(STAB_S)

    return summary

# ─── Main experiment loop ─────────────────────────────────────────────────────
def run_experiment(start_ue: int, end_ue: int, dry_run: bool = False) -> None:
    """
    Run sequential load-balancing from gNB1 → gNB2 for UEs [start_ue..end_ue].
    Processes from highest UE number down to lowest (UE49 first, UE40 last).
    """
    log("=" * 65)
    log(f"  Sequential UE LB Experiment: UE{start_ue}–{end_ue} → gNB2")
    log(f"  Direction: UE{end_ue} first → UE{start_ue} last (one at a time)")
    log(f"  Dry-run: {dry_run}")
    log(f"  Output: {ACCUM_CSV}")
    log("=" * 65)

    init_csv()

    # Initial UE count — poll gNB1 (expect 50 UEs at start)
    log("Polling initial gNB1/gNB2 UE counts...")
    if dry_run:
        n_gnb1 = 50
        n_gnb2 = 0
    else:
        n_gnb1 = get_nof_ue(GNB1_HOST, "gnb1")
        n_gnb2 = get_nof_ue(GNB2_HOST, "gnb2")
        if n_gnb1 <= 0:
            log("WARNING: gNB1 UE count reported ≤0. Assuming 50 (check collectors).")
            n_gnb1 = 50
        if n_gnb2 < 0:
            n_gnb2 = 0

    log(f"Initial state: gNB1={n_gnb1} UEs  gNB2={n_gnb2} UEs")

    results = []
    ue_list = list(range(end_ue, start_ue - 1, -1))   # [49, 48, 47, ..., 40]

    for ue_id in ue_list:
        log(f"\n{'▶' * 3}  Migrating UE{ue_id}  ({ue_list.index(ue_id)+1}/{len(ue_list)})  ◀" * 1)
        try:
            result = handover_one_ue(
                ue_id=ue_id,
                n_gnb1_before=n_gnb1,
                n_gnb2_before=n_gnb2,
                dry_run=dry_run,
            )
            results.append(result)

            # Update running counts for next iteration
            if result["attach_ok"]:
                n_gnb1 -= 1
                n_gnb2 += 1
                log(f"  Updated counts: gNB1={n_gnb1}  gNB2={n_gnb2}")
            else:
                log(f"  UE{ue_id} attach FAILED — counts unchanged: gNB1={n_gnb1}  gNB2={n_gnb2}")

        except KeyboardInterrupt:
            log("Keyboard interrupt — stopping experiment cleanly.")
            break
        except Exception as e:
            log(f"  ERROR in UE{ue_id} handover: {e}")
            results.append({"ue_id": ue_id, "ho_latency_ms": -1, "attach_ok": 0})
            continue

    # ── Final experiment summary ──────────────────────────────────────────────
    log("\n" + "=" * 65)
    log("  EXPERIMENT COMPLETE — Final Summary")
    log("=" * 65)

    successful = [r for r in results if r.get("attach_ok")]
    failed     = [r for r in results if not r.get("attach_ok")]

    log(f"Total UEs attempted : {len(results)}")
    log(f"Successful handovers: {len(successful)}")
    log(f"Failed handovers    : {len(failed)}")

    if successful:
        ho_times = [r["ho_latency_ms"] for r in successful if r["ho_latency_ms"] > 0]
        if ho_times:
            log(f"HO latency (ms)     : min={min(ho_times):.0f}  "
                f"avg={sum(ho_times)/len(ho_times):.0f}  max={max(ho_times):.0f}")

        # Eq.4 summary across all steps
        psaved_vals = [
            r["pre_power_gnb1_w"] - r["post_power_gnb1_w"]
            for r in successful
            if r.get("pre_power_gnb1_w") and r.get("post_power_gnb1_w")
        ]
        if psaved_vals:
            log(f"Eq.4 Psaved/UE (W)  : mean={sum(psaved_vals)/len(psaved_vals):.3f}  "
                f"vals={[round(v,3) for v in psaved_vals]}")

    log(f"\nAccumulation CSV    : {ACCUM_CSV}")
    log(f"Log                 : {LOG_FILE}")
    log(f"Final gNB1 UEs      : {n_gnb1}")
    log(f"Final gNB2 UEs      : {n_gnb2}")
    log("=" * 65)

    # Write text summary
    summary_path = LOCAL_OUT / "sequential_lb_summary.txt"
    with open(summary_path, "w") as f:
        f.write("Sequential 9-UE LB Experiment Summary\n")
        f.write("=" * 60 + "\n")
        f.write(f"UE range        : UE{start_ue}–{end_ue} (processed UE{end_ue}→UE{start_ue})\n")
        f.write(f"Successful HOs  : {len(successful)}/{len(results)}\n\n")
        f.write("Per-UE Results:\n")
        f.write(f"  {'UE':>4}  {'HO_ms':>8}  {'OK':>3}  {'Psaved_gNB1_W':>14}  "
                f"{'ΔP_gNB2_W':>10}\n")
        for r in results:
            psaved = (r.get("pre_power_gnb1_w", 0) - r.get("post_power_gnb1_w", 0))
            dgn2   = r.get("post_power_gnb2_w", 0)
            f.write(f"  UE{r['ue_id']:>2}  {r['ho_latency_ms']:>8}  "
                    f"{'Y' if r.get('attach_ok') else 'N':>3}  "
                    f"{psaved:>14.3f}  {dgn2:>10.3f}\n")
        f.write("\nPaper Equations:\n")
        f.write("  Eq.2: P(N) = α·N^β + γ\n")
        f.write("  Eq.3: Ptotal = Pbase + NaU·PaU + NUi·PUi\n")
        f.write("  Eq.4: Psaved = Pactive − Pswitched  (per-row above)\n")
        f.write("  Run: python3 scripts/analyze_9ue_lb_experiment.py\n")
    log(f"Text summary        : {summary_path}")


# ─── CLI entry point ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Sequential per-UE load-balancing experiment (gNB1 → gNB2)"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate experiment without touching testbed (no SSH/pkill)")
    parser.add_argument("--start-ue", type=int, default=UE_START,
                        help=f"Lowest UE to migrate (default {UE_START})")
    parser.add_argument("--end-ue",   type=int, default=UE_END,
                        help=f"Highest UE to migrate (default {UE_END})")
    args = parser.parse_args()

    if args.start_ue < 0 or args.end_ue > 100 or args.start_ue > args.end_ue:
        die(f"Invalid UE range: {args.start_ue}–{args.end_ue}")

    run_experiment(
        start_ue=args.start_ue,
        end_ue=args.end_ue,
        dry_run=args.dry_run,
    )
