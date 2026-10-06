#!/usr/bin/env python3
"""
5G SA Experiment — Measurement & Results Collection Script
==========================================================
Runs on the POWDER testbed to measure 5G SA performance for both:
  - Experiment A: srsRAN Project 25.10.0 gNB + srsUE 23.04 (ZMQ, Band n3)
  - Experiment B: OAI nr-softmodem 2026.w40 + OAI nrUE 2026.w40 (RFsim, Band n78)

Output CSV columns match the existing 4G LTE template exactly:
  ver, ue_id, n_attached, attach_ms, attach_ok, ue_ip,
  ping_*, dl_*_mbps, dl_*_loss_pct, ul_*_mbps, ul_*_loss_pct,
  ran_*, gnb_ts_*, gnb_cpu_*, phy_*, gnb_*, gnbj_*

Usage (run from any node with SSH access to testbed):
  # Experiment A — srsRAN
  python3 measure_5g.py --exp A --gnb-host pc802 --ue-host pc801 \
    --core-host pc808 --ue-netns ue1 --ue-iface tun_srsue \
    --ver srsran_5g_sa_25_10 --out results/

  # Experiment B — OAI
  python3 measure_5g.py --exp B --gnb-host pc811 --ue-host pc818 \
    --core-host pc808 --ue-netns "" --ue-iface oaitun_ue1 \
    --ver oai_5g_sa_2026w40 --out results/

Prerequisites on gnb/ue nodes:
  sudo apt install -y iperf3 sysstat bc
  iperf3 -s -D   # running on core (pc808)
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

# ---------------------------------------------------------------------------
# Column header — exactly matches 4G template (177 columns)
# ---------------------------------------------------------------------------
COLUMNS = [
    "ver","ue_id","n_attached","attach_ms","attach_ok","ue_ip",
    "ping_avg_ms","ping_min_ms","ping_max_ms","ping_jitter_ms","ping_loss_pct",
    # DL throughput at each UDP target rate (Mbps)
    "dl_1m_mbps","dl_2m_mbps","dl_3m_mbps","dl_4m_mbps","dl_5m_mbps",
    "dl_6m_mbps","dl_7m_mbps","dl_8m_mbps","dl_9m_mbps","dl_10m_mbps",
    "dl_15m_mbps","dl_20m_mbps","dl_25m_mbps","dl_30m_mbps","dl_35m_mbps",
    "dl_40m_mbps","dl_45m_mbps","dl_50m_mbps",
    # DL loss at each UDP target rate (%)
    "dl_1m_loss_pct","dl_2m_loss_pct","dl_3m_loss_pct","dl_4m_loss_pct","dl_5m_loss_pct",
    "dl_6m_loss_pct","dl_7m_loss_pct","dl_8m_loss_pct","dl_9m_loss_pct","dl_10m_loss_pct",
    "dl_15m_loss_pct","dl_20m_loss_pct","dl_25m_loss_pct","dl_30m_loss_pct","dl_35m_loss_pct",
    "dl_40m_loss_pct","dl_45m_loss_pct","dl_50m_loss_pct",
    # UL throughput at each UDP target rate (Mbps)
    "ul_1m_mbps","ul_2m_mbps","ul_3m_mbps","ul_4m_mbps","ul_5m_mbps",
    "ul_6m_mbps","ul_7m_mbps","ul_8m_mbps","ul_9m_mbps","ul_10m_mbps",
    "ul_15m_mbps","ul_20m_mbps","ul_25m_mbps","ul_30m_mbps","ul_35m_mbps",
    "ul_40m_mbps","ul_45m_mbps","ul_50m_mbps",
    # UL loss at each UDP target rate (%)
    "ul_1m_loss_pct","ul_2m_loss_pct","ul_3m_loss_pct","ul_4m_loss_pct","ul_5m_loss_pct",
    "ul_6m_loss_pct","ul_7m_loss_pct","ul_8m_loss_pct","ul_9m_loss_pct","ul_10m_loss_pct",
    "ul_15m_loss_pct","ul_20m_loss_pct","ul_25m_loss_pct","ul_30m_loss_pct","ul_35m_loss_pct",
    "ul_40m_loss_pct","ul_45m_loss_pct","ul_50m_loss_pct",
    # RAN-layer metrics (from gNB console log)
    "ran_dl_mcs","ran_ul_mcs","ran_dl_brate_mbps","ran_ul_brate_mbps",
    "ran_dl_prb","ran_ul_prb","ran_dl_tbs","ran_ul_tbs",
    "ran_dl_bler","ran_ul_bler","ran_cqi","ran_ri","ran_pusch_sinr","ran_phr",
    "ran_nof_ue","ran_system_load","ran_proc_rmem_kb","ran_thread_count","ran_gnb_cpu_avg_pct",
    # gNB turbostat power/thermal (from turbostat during test)
    "gnb_ts_avg_mhz","gnb_ts_busy_pct","gnb_ts_bzy_mhz","gnb_ts_ipc",
    "gnb_ts_irq","gnb_ts_smi","gnb_ts_c1_pct","gnb_ts_c3_pct",
    "gnb_ts_c6_pct","gnb_ts_c7_pct","gnb_ts_core_tmp","gnb_ts_pkg_tmp",
    "gnb_ts_pkg_pc2_pct","gnb_ts_pkg_pc3_pct","gnb_ts_pkg_watt","gnb_ts_ram_watt",
    "gnb_ts_pkg_pct","gnb_ts_ram_pct",
    # gNB CPU / system metrics
    "gnb_cpu_freq_avg_mhz","gnb_cpu_freq_max_mhz","gnb_load1","gnb_load5","gnb_load15",
    "gnb_rapl_pkg0_w","gnb_cpu_user_pct","gnb_cpu_sys_pct","gnb_temp_c",
    "gnb_rapl_pkg1_w","gnb_rapl_total_w","gnb_srsenb_cpu_pct",
    # PHY-layer UE metrics (from srsUE/OAI nrUE console log)
    "phy_dl_mcs_avg","phy_dl_mcs_max","phy_ul_mcs_avg","phy_ul_mcs_max",
    "phy_dl_snr_avg","phy_dl_snr_max","phy_rsrp_avg","phy_pathloss_avg",
    "phy_dl_brate_peak_mbps","phy_dl_brate_avg_mbps",
    "phy_ul_brate_peak_mbps","phy_ul_brate_avg_mbps",
    "phy_dl_bler_avg","phy_ul_bler_avg","phy_dl_turbo_avg","phy_cfo_avg",
    "phy_ul_ta_avg","phy_sample_count",
    # gNB aggregate metrics over test window
    "gnb_dl_brate_peak_mbps","gnb_dl_brate_avg_mbps",
    "gnb_ul_brate_peak_mbps","gnb_ul_brate_avg_mbps",
    "gnb_sys_load_avg","gnb_cpu_avg_pct","gnb_proc_rmem_kb_avg",
    "gnb_thread_count","gnb_sample_count",
    # gNB JSON metrics (srsRAN Project metrics endpoint / OAI REST)
    "gnbj_dl_cqi_avg","gnbj_dl_mcs_avg","gnbj_dl_mcs_max",
    "gnbj_ul_mcs_avg","gnbj_ul_mcs_max","gnbj_ul_snr_avg",
    "gnbj_ul_pusch_rssi_avg","gnbj_ul_pucch_rssi_avg","gnbj_ul_pucch_ni_avg",
    "gnbj_dl_bler_avg","gnbj_ul_bler_avg",
    "gnbj_dl_bitrate_avg_mbps","gnbj_ul_bitrate_avg_mbps","gnbj_ul_phr_avg",
    "gnbj_dl_total_bytes","gnbj_ul_total_bytes","gnbj_dl_latency_avg_ms",
    "gnbj_sample_count",
]
assert len(COLUMNS) == 177, f"Expected 177 columns, got {len(COLUMNS)}"

# UDP target rates in Mbps for DL/UL sweep
UDP_RATES = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 20, 25, 30, 35, 40, 45, 50]

# ---------------------------------------------------------------------------
# SSH helper
# ---------------------------------------------------------------------------
def ssh(host, cmd, timeout=60, user="saish"):
    full = ["ssh", "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            f"{user}@{host}.emulab.net", cmd]
    r = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip(), r.stderr.strip()

def ssh_bg(host, cmd, user="saish"):
    """Start background command, return pid."""
    out, _ = ssh(host, f"nohup bash -c '{cmd}' > /tmp/_bg.log 2>&1 & echo $!", timeout=15, user=user)
    return out.strip()

# ---------------------------------------------------------------------------
# Attach detection
# ---------------------------------------------------------------------------
def wait_for_attach(ue_host, ue_netns, ue_iface, timeout=120):
    """Poll until UE gets an IP on its tunnel interface. Returns (ip, attach_ms) or (None, -1)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if ue_netns:
            cmd = f"sudo ip netns exec {ue_netns} ip -4 addr show {ue_iface} 2>/dev/null | grep 'inet ' | awk '{{print $2}}' | cut -d/ -f1"
        else:
            cmd = f"ip -4 addr show {ue_iface} 2>/dev/null | grep 'inet ' | awk '{{print $2}}' | cut -d/ -f1"
        ip, _ = ssh(ue_host, cmd)
        if ip and ip.startswith("10."):
            return ip, int((time.time() - t0) * 1000)
        time.sleep(2)
    return None, -1

# ---------------------------------------------------------------------------
# Ping test
# ---------------------------------------------------------------------------
def run_ping(ue_host, ue_netns, ue_iface, target="10.45.0.1", count=20):
    if ue_netns:
        cmd = f"sudo ip netns exec {ue_netns} ping {target} -c {count} -I {ue_iface} -q 2>/dev/null"
    else:
        cmd = f"ping {target} -c {count} -I {ue_iface} -q 2>/dev/null"
    out, _ = ssh(ue_host, cmd, timeout=60)
    # Parse: rtt min/avg/max/mdev = X/X/X/X ms
    stats = {"avg": 0.0, "min": 0.0, "max": 0.0, "jitter": 0.0, "loss": 0.0}
    m = re.search(r"(\d+)% packet loss", out)
    if m:
        stats["loss"] = float(m.group(1))
    m = re.search(r"rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)", out)
    if m:
        stats["min"], stats["avg"], stats["max"], stats["jitter"] = [float(x) for x in m.groups()]
    return stats

# ---------------------------------------------------------------------------
# iPerf3 UDP sweep (UL: UE→core, DL: core→UE)
# ---------------------------------------------------------------------------
def iperf3_udp(ue_host, ue_netns, ue_iface, ue_ip, core_host,
               target_mbps, direction="ul", duration=10):
    """
    Returns (achieved_mbps, loss_pct).
    direction: 'ul' = UE→core, 'dl' = core→UE (reverse)
    """
    server_ip = "10.10.1.1"  # core LAN IP

    # Server already running as daemon on core.
    # Client command
    bind = f"-B {ue_ip}" if not ue_netns else ""
    reverse = "-R" if direction == "dl" else ""

    if ue_netns:
        client_cmd = (
            f"sudo ip netns exec {ue_netns} iperf3 -c {server_ip} "
            f"-u -b {target_mbps}M -t {duration} -i 0 {reverse} --json 2>/dev/null"
        )
    else:
        client_cmd = (
            f"iperf3 -c {server_ip} -u -b {target_mbps}M "
            f"-t {duration} -i 0 {bind} {reverse} --json 2>/dev/null"
        )

    out, _ = ssh(ue_host, client_cmd, timeout=duration + 30)
    try:
        data = json.loads(out)
        end = data["end"]
        if direction == "ul":
            bps = end["sum"]["bits_per_second"]
            loss = end["sum"].get("lost_percent", 0.0)
        else:
            # reverse: server stream is in intervals, use sum_received
            bps = end.get("sum_received", end.get("sum", {})).get("bits_per_second", 0)
            loss = end.get("sum_received", end.get("sum", {})).get("lost_percent", 0.0)
        return round(bps / 1e6, 4), round(loss, 4)
    except Exception:
        return 0.0, 0.0

# ---------------------------------------------------------------------------
# gNB log parsing — srsRAN Project console trace
# Format: pci rnti cqi mcs brate ok nok(%) | pusch mcs brate ok nok(%) bsr
# ---------------------------------------------------------------------------
def parse_srsran_gnb_log(gnb_host, log_path="/tmp/gnb1_srsran.log", samples=30):
    cmd = f"grep -E '\\|.*\\|' {log_path} 2>/dev/null | tail -{samples}"
    out, _ = ssh(gnb_host, cmd)
    rows = [l.strip() for l in out.splitlines() if "|" in l and not l.strip().startswith("pci")]
    if not rows:
        return {}

    dl_mcs_vals, ul_mcs_vals = [], []
    dl_brate_vals, ul_brate_vals = [], []
    dl_bler_vals, ul_bler_vals = [], []
    cqi_vals, snr_vals = [], []

    for row in rows:
        # srsRAN Project format:
        # pci rnti cqi mcs brate ok nok(%) | pusch mcs brate ok nok(%) bsr
        parts = re.split(r'\s*\|\s*', row)
        if len(parts) < 2:
            continue
        dl_part = parts[0].split()
        ul_part = parts[1].split()
        try:
            # DL: pci rnti cqi mcs brate ok nok bler%
            if len(dl_part) >= 7:
                cqi_vals.append(float(dl_part[2]))
                dl_mcs_vals.append(float(dl_part[3]))
                brate_str = dl_part[4]
                dl_brate_vals.append(_parse_rate(brate_str))
                bler_str = dl_part[6].replace("%","")
                dl_bler_vals.append(float(bler_str) if bler_str else 0.0)
            # UL: pusch mcs brate ok nok bler% bsr
            if len(ul_part) >= 5:
                snr_vals.append(float(ul_part[0]))
                ul_mcs_vals.append(float(ul_part[1]))
                ul_brate_vals.append(_parse_rate(ul_part[2]))
                bler_str = ul_part[4].replace("%","")
                ul_bler_vals.append(float(bler_str) if bler_str else 0.0)
        except (IndexError, ValueError):
            continue

    def avg(lst): return round(sum(lst)/len(lst), 4) if lst else 0.0
    def peak(lst): return round(max(lst), 4) if lst else 0.0

    return {
        "ran_dl_mcs": avg(dl_mcs_vals),
        "ran_ul_mcs": avg(ul_mcs_vals),
        "ran_dl_brate_mbps": avg(dl_brate_vals),
        "ran_ul_brate_mbps": avg(ul_brate_vals),
        "ran_dl_bler": avg(dl_bler_vals),
        "ran_ul_bler": avg(ul_bler_vals),
        "ran_cqi": avg(cqi_vals),
        "ran_pusch_sinr": avg(snr_vals),
        "gnb_dl_brate_peak_mbps": peak(dl_brate_vals),
        "gnb_dl_brate_avg_mbps": avg(dl_brate_vals),
        "gnb_ul_brate_peak_mbps": peak(ul_brate_vals),
        "gnb_ul_brate_avg_mbps": avg(ul_brate_vals),
        "gnb_sample_count": len(rows),
        # Map to gnbj_ columns for consistency
        "gnbj_dl_cqi_avg": avg(cqi_vals),
        "gnbj_dl_mcs_avg": avg(dl_mcs_vals),
        "gnbj_dl_mcs_max": peak(dl_mcs_vals),
        "gnbj_ul_mcs_avg": avg(ul_mcs_vals),
        "gnbj_ul_mcs_max": peak(ul_mcs_vals),
        "gnbj_ul_snr_avg": avg(snr_vals),
        "gnbj_dl_bler_avg": avg(dl_bler_vals),
        "gnbj_ul_bler_avg": avg(ul_bler_vals),
        "gnbj_dl_bitrate_avg_mbps": avg(dl_brate_vals),
        "gnbj_ul_bitrate_avg_mbps": avg(ul_brate_vals),
        "gnbj_sample_count": len(rows),
    }

def _parse_rate(s):
    """Parse srsRAN rate strings like '13M', '275k', '0.0'."""
    s = s.strip()
    try:
        if s.endswith("M"):
            return float(s[:-1])
        elif s.endswith("k"):
            return float(s[:-1]) / 1e3
        elif s.endswith("G"):
            return float(s[:-1]) * 1e3
        return float(s)
    except ValueError:
        return 0.0

# ---------------------------------------------------------------------------
# gNB log parsing — OAI nr-softmodem
# ---------------------------------------------------------------------------
def parse_oai_gnb_log(gnb_host, log_path="/tmp/gnb2_oai.log", samples=30):
    # OAI prints per-slot stats — extract PHY metrics
    cmd = f"grep -E 'DL.*UL|MCS|BLER|SNR' {log_path} 2>/dev/null | tail -{samples}"
    out, _ = ssh(gnb_host, cmd)
    # Basic extraction — OAI log format varies; use regex on common patterns
    dl_mcs, ul_mcs, dl_bler, ul_bler, snr = [], [], [], [], []
    for line in out.splitlines():
        m = re.search(r'DL.*?MCS\s*(\d+)', line)
        if m: dl_mcs.append(float(m.group(1)))
        m = re.search(r'UL.*?MCS\s*(\d+)', line)
        if m: ul_mcs.append(float(m.group(1)))
        m = re.search(r'DL.*?BLER\s*([\d.]+)', line)
        if m: dl_bler.append(float(m.group(1)))
        m = re.search(r'UL.*?BLER\s*([\d.]+)', line)
        if m: ul_bler.append(float(m.group(1)))
        m = re.search(r'SNR\s*([\d.]+)', line)
        if m: snr.append(float(m.group(1)))

    def avg(lst): return round(sum(lst)/len(lst), 4) if lst else 0.0
    def peak(lst): return round(max(lst), 4) if lst else 0.0

    return {
        "ran_dl_mcs": avg(dl_mcs),
        "ran_ul_mcs": avg(ul_mcs),
        "ran_dl_bler": avg(dl_bler),
        "ran_ul_bler": avg(ul_bler),
        "ran_pusch_sinr": avg(snr),
        "gnbj_dl_mcs_avg": avg(dl_mcs),
        "gnbj_dl_mcs_max": peak(dl_mcs),
        "gnbj_ul_mcs_avg": avg(ul_mcs),
        "gnbj_ul_mcs_max": peak(ul_mcs),
        "gnbj_ul_snr_avg": avg(snr),
        "gnbj_dl_bler_avg": avg(dl_bler),
        "gnbj_ul_bler_avg": avg(ul_bler),
        "gnbj_sample_count": len(out.splitlines()),
    }

# ---------------------------------------------------------------------------
# UE PHY log parsing — srsUE NR console trace
# Format: rat pci rsrp pl cfo | mcs snr iter brate bler ta | mcs buff brate bler
# ---------------------------------------------------------------------------
def parse_srsue_log(ue_host, log_path="/tmp/ue1_srsran.log", samples=30):
    cmd = f"grep -E '^\\s*nr' {log_path} 2>/dev/null | tail -{samples}"
    out, _ = ssh(ue_host, cmd)
    rows = [l.strip() for l in out.splitlines() if l.strip().startswith("nr")]

    rsrp_v, pl_v, cfo_v = [], [], []
    dl_mcs_v, dl_snr_v, dl_brate_v, dl_bler_v, ta_v = [], [], [], [], []
    ul_mcs_v, ul_brate_v, ul_bler_v = [], [], []

    for row in rows:
        parts = re.split(r'\s*\|\s*', row)
        if len(parts) < 3:
            continue
        sig = parts[0].split()    # rat pci rsrp pl cfo
        dl  = parts[1].split()    # mcs snr iter brate bler ta
        ul  = parts[2].split()    # mcs buff brate bler
        try:
            if len(sig) >= 5:
                rsrp_v.append(float(sig[2]))
                pl_v.append(float(sig[3]))
                cfo_v.append(float(sig[4].replace("u","e-6").replace("m","e-3").replace("n","e-9")))
        except: pass
        try:
            if len(dl) >= 6:
                dl_mcs_v.append(float(dl[0]))
                dl_snr_v.append(float(dl[1]))
                dl_brate_v.append(_parse_rate(dl[3]))
                dl_bler_v.append(float(dl[4].replace("%","")))
                ta_v.append(float(dl[5]))
        except: pass
        try:
            if len(ul) >= 4:
                ul_mcs_v.append(float(ul[0]))
                ul_brate_v.append(_parse_rate(ul[2]))
                ul_bler_v.append(float(ul[3].replace("%","")))
        except: pass

    def avg(lst): return round(sum(lst)/len(lst), 4) if lst else 0.0
    def peak(lst): return round(max(lst), 4) if lst else 0.0

    return {
        "phy_rsrp_avg": avg(rsrp_v),
        "phy_pathloss_avg": avg(pl_v),
        "phy_cfo_avg": avg(cfo_v),
        "phy_dl_mcs_avg": avg(dl_mcs_v),
        "phy_dl_mcs_max": peak(dl_mcs_v),
        "phy_dl_snr_avg": avg(dl_snr_v),
        "phy_dl_snr_max": peak(dl_snr_v),
        "phy_dl_brate_avg_mbps": avg(dl_brate_v),
        "phy_dl_brate_peak_mbps": peak(dl_brate_v),
        "phy_dl_bler_avg": avg(dl_bler_v),
        "phy_ul_mcs_avg": avg(ul_mcs_v),
        "phy_ul_mcs_max": peak(ul_mcs_v),
        "phy_ul_brate_avg_mbps": avg(ul_brate_v),
        "phy_ul_brate_peak_mbps": peak(ul_brate_v),
        "phy_ul_bler_avg": avg(ul_bler_v),
        "phy_ul_ta_avg": avg(ta_v),
        "phy_sample_count": len(rows),
    }

# ---------------------------------------------------------------------------
# gNB system metrics (CPU, memory, load)
# ---------------------------------------------------------------------------
def collect_gnb_sysmetrics(gnb_host, gnb_proc="gnb"):
    cmd = (
        f"uptime | awk -F'load average:' '{{print $2}}' | tr -d ' ';"   # load1,5,15
        f"top -bn1 | grep -E '^%Cpu' | awk '{{print $2,$4}}';"           # user sys
        f"ps -C {gnb_proc} -o rss= 2>/dev/null | awk '{{s+=$1}}END{{print s}}';"  # rss KB
        f"ps -C {gnb_proc} -o nlwp= 2>/dev/null | tail -1;"              # threads
        f"cat /proc/cpuinfo | grep 'cpu MHz' | awk '{{sum+=$4;n++}}END{{print sum/n,\" \",max}}' 2>/dev/null || echo 0 0"
    )
    out, _ = ssh(gnb_host, cmd)
    lines = out.splitlines()
    result = {}
    try:
        loads = lines[0].strip().split(",")
        result["gnb_load1"] = float(loads[0]) if loads else 0.0
        result["gnb_load5"] = float(loads[1]) if len(loads)>1 else 0.0
        result["gnb_load15"] = float(loads[2]) if len(loads)>2 else 0.0
    except: pass
    try:
        cpu = lines[1].strip().split()
        result["gnb_cpu_user_pct"] = float(cpu[0]) if cpu else 0.0
        result["gnb_cpu_sys_pct"] = float(cpu[1]) if len(cpu)>1 else 0.0
    except: pass
    try: result["gnb_proc_rmem_kb"] = float(lines[2].strip() or 0)
    except: pass
    try: result["gnb_thread_count"] = int(lines[3].strip() or 0)
    except: pass
    try:
        freq = lines[4].strip().split()
        result["gnb_cpu_freq_avg_mhz"] = float(freq[0]) if freq else 0.0
    except: pass
    # CPU temp
    temp_out, _ = ssh(gnb_host, "cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | head -1")
    try: result["gnb_temp_c"] = float(temp_out.strip()) / 1000.0
    except: result["gnb_temp_c"] = 0.0
    return result

# ---------------------------------------------------------------------------
# Main measurement loop
# ---------------------------------------------------------------------------
def run_experiment(args):
    ver = args.ver
    gnb_host = args.gnb_host
    ue_host  = args.ue_host
    core_host = args.core_host
    ue_netns = args.ue_netns
    ue_iface = args.ue_iface
    gnb_log  = args.gnb_log
    ue_log   = args.ue_log
    gnb_proc = args.gnb_proc
    out_dir  = args.out

    os.makedirs(out_dir, exist_ok=True)
    out_csv = os.path.join(out_dir, f"{ver}_results.csv")

    print(f"\n{'='*60}")
    print(f" 5G SA Experiment — {ver}")
    print(f" gNB: {gnb_host}  UE: {ue_host}  Core: {core_host}")
    print(f" UE iface: {ue_iface}  netns: {ue_netns or 'none'}")
    print(f" Output: {out_csv}")
    print(f"{'='*60}\n")

    # --- Wait for UE attachment ---
    print("[1/5] Waiting for UE attachment...")
    t_attach_start = time.time()
    ue_ip, attach_ms = wait_for_attach(ue_host, ue_netns, ue_iface, timeout=180)
    if not ue_ip:
        print("  ERROR: UE did not attach within 180s. Check gNB and UE processes.")
        sys.exit(1)
    print(f"  UE IP: {ue_ip}  attach_ms: {attach_ms}")

    # Count attached UEs (only 1 in 5G SA experiment)
    n_attached = 1

    # --- Ensure iPerf3 server is running on core ---
    ssh(core_host, "pkill iperf3 2>/dev/null; sleep 1; iperf3 -s -D --forking", timeout=15)
    time.sleep(2)

    # --- Ping test ---
    print("[2/5] Running ping test (20 packets → 10.45.0.1)...")
    ping = run_ping(ue_host, ue_netns, ue_iface, target="10.45.0.1", count=20)
    print(f"  avg={ping['avg']}ms  min={ping['min']}ms  max={ping['max']}ms  "
          f"jitter={ping['jitter']}ms  loss={ping['loss']}%")

    # --- UDP throughput sweep ---
    print("[3/5] Running UDP throughput sweep (DL + UL, 18 rates × 10s each)...")
    dl_mbps, dl_loss, ul_mbps, ul_loss = {}, {}, {}, {}

    for rate in UDP_RATES:
        key = f"{rate}m"
        print(f"  DL {rate} Mbps ... ", end="", flush=True)
        mbps, loss = iperf3_udp(ue_host, ue_netns, ue_iface, ue_ip,
                                 core_host, rate, direction="dl", duration=10)
        dl_mbps[key] = mbps; dl_loss[key] = loss
        print(f"{mbps:.3f} Mbps  loss={loss:.2f}%")

        print(f"  UL {rate} Mbps ... ", end="", flush=True)
        mbps, loss = iperf3_udp(ue_host, ue_netns, ue_iface, ue_ip,
                                 core_host, rate, direction="ul", duration=10)
        ul_mbps[key] = mbps; ul_loss[key] = loss
        print(f"{mbps:.3f} Mbps  loss={loss:.2f}%")

    # --- Parse gNB console log ---
    print("[4/5] Parsing gNB and UE logs...")
    if "oai" in ver.lower():
        gnb_metrics = parse_oai_gnb_log(gnb_host, gnb_log)
    else:
        gnb_metrics = parse_srsran_gnb_log(gnb_host, gnb_log)

    phy_metrics = parse_srsue_log(ue_host, ue_log) if "srsran" in ver.lower() else {}

    # --- gNB system metrics ---
    print("[5/5] Collecting gNB system metrics...")
    sys_metrics = collect_gnb_sysmetrics(gnb_host, gnb_proc)

    # --- Assemble row ---
    row = {c: 0.0 for c in COLUMNS}
    row["ver"] = ver
    row["ue_id"] = 1
    row["n_attached"] = n_attached
    row["attach_ms"] = attach_ms
    row["attach_ok"] = "OK" if ue_ip else "FAIL"
    row["ue_ip"] = ue_ip or ""
    row["ping_avg_ms"] = ping["avg"]
    row["ping_min_ms"] = ping["min"]
    row["ping_max_ms"] = ping["max"]
    row["ping_jitter_ms"] = ping["jitter"]
    row["ping_loss_pct"] = ping["loss"]

    for rate in UDP_RATES:
        key = f"{rate}m"
        row[f"dl_{key}_mbps"]      = dl_mbps.get(key, 0.0)
        row[f"dl_{key}_loss_pct"]  = dl_loss.get(key, 0.0)
        row[f"ul_{key}_mbps"]      = ul_mbps.get(key, 0.0)
        row[f"ul_{key}_loss_pct"]  = ul_loss.get(key, 0.0)

    row["ran_nof_ue"] = n_attached
    for k, v in gnb_metrics.items():
        if k in row: row[k] = v
    for k, v in phy_metrics.items():
        if k in row: row[k] = v
    for k, v in sys_metrics.items():
        if k in row: row[k] = v

    # --- Write CSV ---
    file_exists = os.path.isfile(out_csv)
    with open(out_csv, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

    print(f"\n✓ Result written to: {out_csv}")
    print(f"  DL peak: {max(dl_mbps.values()):.2f} Mbps  "
          f"UL peak: {max(ul_mbps.values()):.2f} Mbps  "
          f"Ping: {ping['avg']:.1f} ms")
    return out_csv

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="5G SA experiment measurement script")
    ap.add_argument("--exp",        choices=["A","B"], required=True,
                    help="A=srsRAN, B=OAI")
    ap.add_argument("--gnb-host",   default="pc802",
                    help="gNB Emulab hostname (without .emulab.net)")
    ap.add_argument("--ue-host",    default="pc801",
                    help="UE Emulab hostname")
    ap.add_argument("--core-host",  default="pc808",
                    help="Core Emulab hostname")
    ap.add_argument("--ue-netns",   default="ue1",
                    help="UE network namespace (empty string for OAI)")
    ap.add_argument("--ue-iface",   default="tun_srsue",
                    help="UE tunnel interface name")
    ap.add_argument("--gnb-log",    default="/tmp/gnb1_srsran.log",
                    help="Path to gNB log on gnb-host")
    ap.add_argument("--ue-log",     default="/tmp/ue1_srsran.log",
                    help="Path to UE log on ue-host")
    ap.add_argument("--gnb-proc",   default="gnb",
                    help="gNB process name for CPU/mem stats")
    ap.add_argument("--ver",        default=None,
                    help="Version label for CSV (auto-set if not given)")
    ap.add_argument("--out",        default="results",
                    help="Output directory for CSV")
    args = ap.parse_args()

    # Auto-set ver and defaults per experiment
    if args.exp == "A":
        if not args.ver: args.ver = "srsran_5g_sa_25_10"
        if args.gnb_host == "pc802" and args.gnb_log == "/tmp/gnb1_srsran.log":
            pass  # defaults OK
    else:  # B
        if not args.ver: args.ver = "oai_5g_sa_2026w40"
        if args.gnb_host == "pc802":
            args.gnb_host = "pc811"
        if args.ue_host == "pc801":
            args.ue_host = "pc818"
        if args.ue_netns == "ue1":
            args.ue_netns = ""
        if args.ue_iface == "tun_srsue":
            args.ue_iface = "oaitun_ue1"
        if args.gnb_log == "/tmp/gnb1_srsran.log":
            args.gnb_log = "/tmp/gnb2_oai.log"
        if args.ue_log == "/tmp/ue1_srsran.log":
            args.ue_log = "/tmp/ue2_oai.log"
        if args.gnb_proc == "gnb":
            args.gnb_proc = "nr-softmodem"

    run_experiment(args)

if __name__ == "__main__":
    main()
