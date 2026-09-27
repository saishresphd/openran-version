#!/usr/bin/env python3
"""
ver_exp_gnb_50ue_fixed.py — runs on pc802 (gnb1 host)

POWDER node mapping (2025 allocation):
  core     = pc808  10.10.1.1   Open5GS EPC
  gnb1     = pc802  10.10.1.2   ← this node
  uehost1  = pc801  10.10.1.4   UE ZMQ connect target

Starts 50 srsenb ZMQ slots for the requested srsRAN version, collects
turbostat CPU/power snapshots every 8 s while UEs ramp, and writes
gnb_snapshots_50ue.csv.

Fixes vs ver_exp_gnb_50ue.py:
  1. turbostat `--interval` replaced with `sleep N` form (more portable).
  2. Summary row selection: use the LAST non-empty data row, not the first —
     turbostat prints a PKG/package-level summary as the last row.
  3. SMI column made optional (absent on non-Intel or virtualised nodes).
  4. RAPL reads fall back to /sys reads without sudo when readable.
  5. Load averages and /proc freq stats always collected as fallback.

Usage (on pc802):
    python3 ver_exp_gnb_50ue_fixed.py v22_10
"""

import sys, os, subprocess, time, glob, csv

# ── Node constants ────────────────────────────────────────────────────────────
UEHOST1_IP = "10.10.1.4"   # UE ZMQ rx_port target

VER    = sys.argv[1] if len(sys.argv) > 1 else "v22_10"
N_UE   = 50

ENB_BINS = {
    "v21_04": "/opt/srsran_2104/bin/srsenb",
    "v21_10": "/opt/srsran_2110/bin/srsenb",
    "v22_10": "/opt/srsran_22_10/bin/srsenb",
    "v23_11": "/opt/srsran_23_11/bin/srsenb",
    "v25_10": "/opt/srsran_2510/bin/srsenb",
}
ENB_BIN   = ENB_BINS[VER]
CFG_DIR   = f"/tmp/ver_configs_50ue/{VER}"
OUT_DIR   = f"/tmp/ver_eval_50ue/{VER}"
SENTINEL  = f"{OUT_DIR}/UE_DONE"
SNAP_FILE = f"{OUT_DIR}/gnb_snapshots_50ue.csv"

# ── turbostat column map: turbostat-header → our CSV column name ──────────────
# Only columns that turbostat actually emits on Intel Xeon server hardware.
# Columns that are absent on this platform are silently zeroed.
TS_COL_MAP = {
    "PkgWatt":   "ts_pkg_watt",
    "CorWatt":   "ts_cor_watt",
    "RAMWatt":   "ts_ram_watt",
    "GFXWatt":   "ts_gfx_watt",
    "Avg_MHz":   "ts_avg_mhz",
    "Busy%":     "ts_busy_pct",
    "Bzy_MHz":   "ts_bzy_mhz",
    "IPC":       "ts_ipc",
    "IRQ":       "ts_irq",
    "SMI":       "ts_smi",
    "POLL%":     "ts_poll_pct",
    "CPU%c1":    "ts_c1_pct",
    "CPU%c1E":   "ts_c1e_pct",
    "CPU%c3":    "ts_c3_pct",
    "CPU%c6":    "ts_c6_pct",
    "CPU%c7":    "ts_c7_pct",
    "CPU%c8":    "ts_c8_pct",
    "CPU%c10":   "ts_c10_pct",
    "CoreTmp":   "ts_core_tmp",
    "PkgTmp":    "ts_pkg_tmp",
    "Pkg%pc2":   "ts_pkg_pc2_pct",
    "Pkg%pc3":   "ts_pkg_pc3_pct",
    "Pkg%pc6":   "ts_pkg_pc6_pct",
    "Pkg%pc8":   "ts_pkg_pc8_pct",
}
TS_CSV_COLS = list(TS_COL_MAP.values())

TURBOSTAT_AVAILABLE = (
    subprocess.run("which turbostat", shell=True, capture_output=True).returncode == 0
)

os.makedirs(f"{OUT_DIR}/gnb_logs", exist_ok=True)

# ── Helpers ───────────────────────────────────────────────────────────────────
def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}][{VER}] {msg}", flush=True)

def run(cmd, **kw):
    return subprocess.run(cmd, shell=True, **kw)

def kill_all():
    run("sudo pkill -9 srsenb 2>/dev/null || true")
    time.sleep(2)

# ── turbostat snapshot ────────────────────────────────────────────────────────
def turbostat_snap() -> dict:
    """
    Run turbostat for a 2-second interval and return a dict of system-summary values.
    Uses the `sleep 2` form (not --interval) for broader compatibility.
    Parses the LAST non-empty data row as the package-level summary.
    """
    empty = {v: 0.0 for v in TS_CSV_COLS}
    if not TURBOSTAT_AVAILABLE:
        return empty

    # Build comma-separated show list from keys we care about
    show_cols = ",".join(TS_COL_MAP.keys())
    cmd = f"sudo turbostat --quiet --show {show_cols} sleep 2 2>/dev/null"
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=12)
    except subprocess.TimeoutExpired:
        return empty

    lines = [l for l in r.stdout.splitlines() if l.strip()]
    if len(lines) < 2:
        return empty

    # Header line contains column names
    hdr_idx = next(
        (i for i, l in enumerate(lines)
         if any(k in l for k in ("PkgWatt", "Busy%", "Avg_MHz"))),
        -1)
    if hdr_idx < 0 or hdr_idx + 1 >= len(lines):
        return empty

    hdrs = lines[hdr_idx].split()

    # The summary (package-total) row is the LAST non-empty data row
    data_rows = [l.split() for l in lines[hdr_idx + 1:] if l.strip()]
    if not data_rows:
        return empty
    # Pick the row with the most columns matching the header (the summary row
    # usually has the most populated fields on multi-socket systems)
    data_row = max(data_rows, key=lambda r: len(r))

    row_map: dict = {}
    for h, v in zip(hdrs, data_row):
        try:
            row_map[h] = float(v)
        except ValueError:
            row_map[h] = 0.0

    result = {v: 0.0 for v in TS_CSV_COLS}
    for ts_key, csv_col in TS_COL_MAP.items():
        result[csv_col] = row_map.get(ts_key, 0.0)
    return result

# ── /proc CPU + RAPL fallback ─────────────────────────────────────────────────
def cpu_sample():
    def p(line):
        f = line.split()
        v = [int(x) for x in f[1:9]]
        return sum(v), v[3], v[0] + v[1], v[2]
    l1 = [l for l in open("/proc/stat") if l.startswith("cpu ")][0]
    time.sleep(1)
    l2 = [l for l in open("/proc/stat") if l.startswith("cpu ")][0]
    t1, i1, u1, s1 = p(l1)
    t2, i2, u2, s2 = p(l2)
    dt = (t2 - t1) or 1
    return round(100 * (u2 - u1) / dt, 1), round(100 * (s2 - s1) / dt, 1)

def rapl_w(path):
    """Read RAPL energy counter power (W) over a 1 s window."""
    try:
        # Try direct read first (works when running as root or with ACL)
        e0 = int(open(path).read().strip())
        time.sleep(1)
        e1 = int(open(path).read().strip())
        return round((e1 - e0) / 1e6, 3)
    except PermissionError:
        pass
    try:
        r0 = subprocess.run(["sudo", "cat", path], capture_output=True, text=True, timeout=2)
        e0 = int(r0.stdout.strip())
        time.sleep(1)
        r1 = subprocess.run(["sudo", "cat", path], capture_output=True, text=True, timeout=2)
        e1 = int(r1.stdout.strip())
        return round((e1 - e0) / 1e6, 3)
    except Exception:
        return 0.0

def load_avg():
    try:
        parts = open("/proc/loadavg").read().split()
        return float(parts[0]), float(parts[1]), float(parts[2])
    except Exception:
        return 0.0, 0.0, 0.0

def proc_cpu_freq():
    try:
        freqs = []
        for fp in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq"):
            try:
                freqs.append(int(open(fp).read().strip()) / 1000)
            except Exception:
                pass
        if not freqs:
            return 0.0, 0.0
        return round(sum(freqs) / len(freqs), 1), round(max(freqs), 1)
    except Exception:
        return 0.0, 0.0

def irq_rate():
    try:
        def intr():
            for l in open("/proc/stat"):
                if l.startswith("intr"):
                    return int(l.split()[1])
            return 0
        i0 = intr(); time.sleep(0.3); i1 = intr()
        return int((i1 - i0) / 0.3)
    except Exception:
        return 0

def temp_c():
    try:
        ts = [int(open(f).read()) / 1000
              for f in glob.glob("/sys/class/thermal/thermal_zone*/temp")]
        return round(max(ts), 1) if ts else 0
    except Exception:
        return 0

# ── RAN metrics from the most recently active srsenb metrics CSV ──────────────
def ran_metrics(n_active: int) -> dict:
    ran = dict(
        ran_dl_brate_mbps=0, ran_ul_brate_mbps=0, ran_nof_ue=0,
        ran_ul_snr_db=0, ran_pucch_cqi=0,
        ran_dl_mcs=0, ran_ul_mcs=0,
        ran_dl_prb=0, ran_ul_prb=0,
        ran_dl_bler=0, ran_ul_bler=0,
    )
    try:
        mcsv = f"{OUT_DIR}/gnb_ue{max(1, n_active)}_metrics.csv"
        lines = open(mcsv).readlines()
        if len(lines) < 3:
            return ran
        hdr  = lines[0].strip().split(";")
        rows = [dict(zip(hdr, l.strip().split(";"))) for l in lines[-5:] if l.strip()]

        def av(k):
            vals = [float(r.get(k, 0) or 0) for r in rows if r.get(k, "")]
            return round(sum(vals) / len(vals), 4) if vals else 0

        ran["ran_nof_ue"]          = av("nof_ue")
        ran["ran_dl_brate_mbps"]   = round(av("dl_brate") / 1e6, 4)
        ran["ran_ul_brate_mbps"]   = round(av("ul_brate") / 1e6, 4)
        ran["ran_dl_mcs"]          = av("dl_mcs")
        ran["ran_ul_mcs"]          = av("ul_mcs")
        ran["ran_dl_prb"]          = av("dl_prb")
        ran["ran_ul_prb"]          = av("ul_prb")
        ran["ran_dl_bler"]         = av("dl_bler")
        ran["ran_ul_bler"]         = av("ul_bler")
        ran["ran_ul_snr_db"]       = av("ul_snr")
        ran["ran_pucch_cqi"]       = av("pucch_cqi")
    except Exception:
        pass
    return ran

# ── Full snapshot (called every 8 s while UEs ramp) ──────────────────────────
def snap_metrics(n_active: int) -> dict:
    ts   = turbostat_snap()
    la1, la5, la15 = load_avg()
    cpu_u, cpu_s   = cpu_sample()          # /proc CPU breakdown
    irq            = irq_rate()
    tmp            = temp_c()
    pkg0 = rapl_w("/sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj")
    dram = rapl_w("/sys/class/powercap/intel-rapl/intel-rapl:0/intel-rapl:0:0/energy_uj")

    if TURBOSTAT_AVAILABLE:
        freq_avg = ts.get("ts_avg_mhz", 0.0)
        freq_max = ts.get("ts_bzy_mhz", 0.0)
    else:
        freq_avg, freq_max = proc_cpu_freq()

    return {
        "n_active":          n_active,
        "cpu_user_pct":      cpu_u,
        "cpu_sys_pct":       cpu_s,
        "load1":             la1,
        "load5":             la5,
        "load15":            la15,
        "cpu_freq_avg_mhz":  freq_avg,
        "cpu_freq_max_mhz":  freq_max,
        "rapl_pkg0_W":       pkg0,
        "rapl_dram_W":       dram,
        "irq_rate":          irq,
        "temp_c":            tmp,
        **ts,
        **ran_metrics(n_active),
    }

# ── CSV column order ──────────────────────────────────────────────────────────
HDR_COLS = (
    ["n_active",
     "cpu_user_pct", "cpu_sys_pct",
     "load1", "load5", "load15",
     "cpu_freq_avg_mhz", "cpu_freq_max_mhz",
     "rapl_pkg0_W", "rapl_dram_W",
     "irq_rate", "temp_c"]
    + TS_CSV_COLS
    + ["ran_dl_brate_mbps", "ran_ul_brate_mbps", "ran_nof_ue",
       "ran_ul_snr_db", "ran_pucch_cqi",
       "ran_dl_mcs", "ran_ul_mcs",
       "ran_dl_prb", "ran_ul_prb",
       "ran_dl_bler", "ran_ul_bler"]
)

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log("Killing any previous srsenb processes...")
    kill_all()

    log(f"Starting {N_UE} srsenb slots for {VER}...")
    procs = []
    for n in range(1, N_UE + 1):
        cfg    = f"{CFG_DIR}/enb_ue{n}.conf"
        mcsv   = f"{OUT_DIR}/gnb_ue{n}_metrics.csv"
        logf   = f"{OUT_DIR}/gnb_logs/gnb_ue{n}.log"
        stdout = open(f"{OUT_DIR}/gnb_logs/gnb_ue{n}_stdout.log", "w")
        cmd = (f"sudo {ENB_BIN} {cfg} "
               f"--expert.metrics_csv_filename={mcsv} "
               f"--log.filename={logf}")
        p = subprocess.Popen(cmd, shell=True, stdout=stdout, stderr=stdout)
        procs.append((n, p))
        time.sleep(0.1)

    log("Waiting 15s for ZMQ ports to bind...")
    time.sleep(15)

    r = run("ss -tnlp | grep :40010", capture_output=True, text=True)
    if ":40010" not in r.stdout:
        log("ERROR: port 40010 not bound. Dumping gnb_ue1 stdout:")
        try:
            print(open(f"{OUT_DIR}/gnb_logs/gnb_ue1_stdout.log").read()[-2000:])
        except Exception:
            pass
        open(f"{OUT_DIR}/gnb_ready", "w").write("FAIL")
        sys.exit(1)

    bound = int(
        run("ss -tnlp | grep -cE ':400[0-9][0-9] ' || echo 0",
            capture_output=True, text=True).stdout.strip() or 0)
    log(f"gNB ready: {bound} ZMQ ports bound (expected {N_UE})")
    open(f"{OUT_DIR}/gnb_ready", "w").write("OK")

    if not TURBOSTAT_AVAILABLE:
        log("WARNING: turbostat not found — ts_* columns will be 0. "
            "Install: sudo apt install linux-tools-common linux-tools-$(uname -r)")

    # Write CSV header
    with open(SNAP_FILE, "w", newline="") as sf:
        w = csv.DictWriter(sf, fieldnames=HDR_COLS)
        w.writeheader()

    log(f"Polling for SENTINEL ({SENTINEL}) — snapping every 8s...")
    n_active = 0
    while not os.path.exists(SENTINEL):
        try:
            n_active = int(open(f"{OUT_DIR}/ue_progress").read().strip())
        except Exception:
            pass
        snap = snap_metrics(n_active)
        with open(SNAP_FILE, "a", newline="") as sf:
            w = csv.DictWriter(sf, fieldnames=HDR_COLS)
            w.writerow(snap)
        log(f"  snap: active={n_active} "
            f"pkg={snap['ts_pkg_watt']}W(ts)/{snap['rapl_pkg0_W']}W(rapl) "
            f"busy={snap['ts_busy_pct']}% "
            f"freq_avg={snap['cpu_freq_avg_mhz']}MHz "
            f"load={snap['load1']}/{snap['load5']}/{snap['load15']} "
            f"c6={snap['ts_c6_pct']}% "
            f"dl={snap['ran_dl_brate_mbps']}Mbps")
        time.sleep(8)

    log(f"SENTINEL seen — gNB collection done. Snapshots → {SNAP_FILE}")
    kill_all()
