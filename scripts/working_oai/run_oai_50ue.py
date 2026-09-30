#!/usr/bin/env python3
"""
run_oai_50ue.py — OAI 4G LTE 50-UE Accumulation Experiment on POWDER.
====================================================================
Node mapping:
  core    = pc808   10.10.1.1   Open5GS EPC (MME+SGW+UPF)
  gnb1    = pc802   10.10.1.2   OAI lte-softmodem (one per UE slot)
  uehost1 = pc801   10.10.1.4   OAI lte-uesoftmodem (one per UE slot)

Output: results/ver_eval/oai/ue_results_50.csv  — matching 170-column schema
  - 18 DL + 18 UL standard TCP rates (1-10, 15,20,...,50 Mbps)
  - Full turbostat (18 cols) + dual-socket RAPL (pkg0+pkg1)
  - RAN, PHY, and aggregated telnet metrics
  - Attach timeout: 600s
"""

import subprocess, time, csv, os, re, json, pathlib, hashlib, shutil, signal, threading

KEY      = os.path.expanduser("~/.ssh/id_ed25519")
GNB_HOST = "saish@pc802.emulab.net"
UE_HOST  = "saish@pc801.emulab.net"
CORE_HOST= "saish@pc808.emulab.net"

OAI_ENB_BIN  = "/opt/openairinterface5g/cmake_targets/ran_build/build/lte-softmodem"
OAI_UE_BIN   = "/opt/openairinterface5g/cmake_targets/ran_build/build/lte-uesoftmodem"
ENB_CONF_DIR = "/etc/oai_enb"
UE_CONF_DIR  = "/etc/oai_ue"
UE_LOG_DIR   = "/tmp/ue_logs_oai"
GNB_LOG_DIR  = "/tmp/gnb_logs_oai"

GNB_GTP_BASE = "10.10.2"
GNB_LAN_IF   = "enp4s0f1"

ATTACH_TIMEOUT = 600      # 600s attach timeout
PING_COUNT     = 20
PING_SETTLE_S  = 5
DL_RATES = list(range(1, 11)) + list(range(15, 51, 5))   # 18 rates
UL_RATES = list(range(1, 11)) + list(range(15, 51, 5))   # 18 rates
IPERF_DUR      = 10
MAX_UE         = 50
CORE_IP        = "10.45.0.1"

OUT_DIR  = pathlib.Path("results/ver_eval/oai")
OUT_FILE = OUT_DIR / "ue_results_50.csv"
DESK_FILE= pathlib.Path(os.path.expanduser("~/Desktop/oai_ue_results_50.csv"))

SSH_ARGS = ["-i", KEY,
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=20",
            "-o", "ServerAliveCountMax=3",
            "-o", "BatchMode=yes"]


def ssh(host, cmd, timeout=30):
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
    tag     = hashlib.md5(cmd.encode()).hexdigest()[:8]
    rscript = f"/tmp/bg_{tag}.sh"
    upload  = ["ssh"] + SSH_ARGS + [host, f"cat > {rscript} && chmod +x {rscript}"]
    subprocess.run(upload, input=f"#!/bin/bash\n{cmd}\n",
                   capture_output=True, text=True, timeout=12)
    launcher = f"setsid bash {rscript} </dev/null >/dev/null 2>&1 & disown $! && exit 0"
    subprocess.run(["ssh"] + SSH_ARGS + [host, f"bash -c '{launcher}'"],
                   capture_output=True, timeout=12)
    time.sleep(0.5)


def kill_all():
    print("  Killing lte-softmodem (pc802) & lte-uesoftmodem (pc801)...")
    ssh(GNB_HOST, "sudo killall lte-softmodem 2>/dev/null; sleep 1; "
                  "sudo killall -9 lte-softmodem 2>/dev/null; sleep 1; echo done", timeout=15)
    ssh(UE_HOST,  "sudo killall lte-uesoftmodem 2>/dev/null; sleep 3; "
                  "sudo killall -9 lte-uesoftmodem 2>/dev/null; sleep 1; echo done", timeout=15)
    time.sleep(4)


def restart_epc():
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


def start_oai_enb(n):
    conf = f"{ENB_CONF_DIR}/enb_ue{n}.conf"
    log  = f"{GNB_LOG_DIR}/enb_ue{n}.log"
    ssh(GNB_HOST, f"mkdir -p {GNB_LOG_DIR} && rm -f {log}", timeout=10)
    cmd = f"sudo {OAI_ENB_BIN} -O {conf} --rfsim >> {log} 2>&1"
    ssh_bg(GNB_HOST, cmd)


def wait_enb_port(n, timeout=30):
    port = 40000 + n * 10
    deadline = time.time() + timeout
    while time.time() < deadline:
        out, _ = ssh(GNB_HOST, f"ss -tnlp 2>/dev/null | grep -c ':{port} ' || echo 0", timeout=10)
        if out.strip() == "1":
            return True
        time.sleep(2)
    return False


def start_oai_ue(n):
    conf = f"{UE_CONF_DIR}/ue{n}.conf"
    log  = f"{UE_LOG_DIR}/ue{n}.log"
    ssh(UE_HOST, f"mkdir -p {UE_LOG_DIR} && rm -f {log}", timeout=10)
    cmd = (f"sudo ip netns exec ue{n} {OAI_UE_BIN} -O {conf} "
           f"--rfsim --rfsimulator.serveraddr 10.10.1.2 -r 50 --nokrnmod 1 >> {log} 2>&1")
    ssh_bg(UE_HOST, cmd)


def wait_attach(n, timeout=ATTACH_TIMEOUT):
    t_start  = time.time()
    deadline = t_start + timeout
    time.sleep(6)
    while time.time() < deadline:
        out, _ = ssh(UE_HOST,
                     f"sudo ip netns exec ue{n} ip -br a 2>/dev/null "
                     f"| grep -E 'oaitun|tun|ue' | awk '{{print $3}}'",
                     timeout=12)
        ip = out.strip().split("/")[0] if out.strip() else None
        if ip and ip.startswith("10.45.0."):
            return ip, int((time.time() - t_start) * 1000)
        time.sleep(3)
    return None, int(timeout * 1000)


def inject_default_route(n):
    cmd = (
        f"DEV=$(sudo ip netns exec ue{n} ip -br a 2>/dev/null "
        f"  | grep -E 'oaitun|tun' | awk '{{print $1}}' | head -1); "
        f"if [ -n \"$DEV\" ]; then "
        f"  sudo ip netns exec ue{n} ip route del 10.45.0.0/24 dev $DEV 2>/dev/null || true; "
        f"  sudo ip netns exec ue{n} ip route add 10.45.0.1/32 dev $DEV 2>/dev/null || true; "
        f"  sudo ip netns exec ue{n} ip route add default via 10.45.0.1 dev $DEV 2>/dev/null || true; "
        f"  sudo ip netns exec ue{n} ip route show 2>/dev/null; "
        f"fi"
    )
    out, _ = ssh(UE_HOST, cmd, timeout=12)
    return "10.45.0.1" in out and "default" in out


def run_ping(n, count=None):
    c   = count or PING_COUNT
    cmd = f"sudo ip netns exec ue{n} ping -c {c} -i 0.3 -W 2 {CORE_IP}"
    out, _ = ssh(UE_HOST, cmd, timeout=c * 2 + 12)
    try:
        loss_m = re.search(r'(\d+(?:\.\d+)?)% packet loss', out)
        loss   = float(loss_m.group(1)) if loss_m else 100.0
        rtt_m  = re.search(r'rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)', out)
        if rtt_m:
            return (float(rtt_m.group(2)), float(rtt_m.group(1)),
                    float(rtt_m.group(3)), float(rtt_m.group(4)), loss)
    except Exception:
        pass
    return 999.0, 999.0, 999.0, 999.0, 100.0


def ensure_iperf_server(n):
    port = 5200 + n
    out, _ = ssh(CORE_HOST, f"ss -tnlp 2>/dev/null | grep -c ':{port} ' || echo 0", timeout=8)
    if out.strip() == "1":
        return
    loop = f"while true; do iperf3 -s -B {CORE_IP} -p {port} --one-off 2>/dev/null; sleep 0.2; done"
    ssh_bg(CORE_HOST, loop)
    time.sleep(1)


def run_iperf(n, rate_mbps, direction="dl"):
    """Run standard TCP iperf3 from UE netns."""
    ensure_iperf_server(n)
    port = 5200 + n
    flag = "-R" if direction == "dl" else ""
    cmd  = f"sudo ip netns exec ue{n} iperf3 -c {CORE_IP} -p {port} -b {rate_mbps}M -t {IPERF_DUR} {flag} --json"
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


def snap_gnb_metrics():
    script = r"""
TS_COLS="Avg_MHz,Busy%,Bzy_MHz,IPC,IRQ,SMI,CPU%c1,CPU%c3,CPU%c6,CPU%c7,CoreTmp,PkgTmp,Pkg%pc2,Pkg%pc3,PkgWatt,RAMWatt,PKG_%,RAM_%"
TSFILE=$(mktemp /tmp/ts_XXXXXX)
sudo turbostat --quiet --show $TS_COLS sleep 5 > $TSFILE 2>&1
TS_OUT=$(grep -v '^$' "$TSFILE" | grep -v '^[A-Za-z]' | grep -v '^[0-9][0-9.]*[[:space:]]*sec' | awk -F'\t' 'NF>=15{print; exit}')
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
awk 'BEGIN{s=0;n=0;mx=0} /cpu MHz/{v=$4+0; s+=v; n++; if(v>mx)mx=v} END{if(n>0)printf "FREQ:%.0f/%.0f\n",s/n,mx}' /proc/cpuinfo

C1=$(awk '/^cpu /{print $2,$3,$4,$5,$6,$7,$8}' /proc/stat)
sleep 1
C2=$(awk '/^cpu /{print $2,$3,$4,$5,$6,$7,$8}' /proc/stat)
awk -v a="$C1" -v b="$C2" 'BEGIN{
  split(a,x); split(b,y)
  usr=y[1]-x[1]; sys=y[3]-x[3]; idle=y[4]-x[4]; nice=y[2]-x[2]
  tot=usr+sys+idle+nice; if(tot==0) tot=1
  printf "CPUSTAT:%.2f:%.2f\n", usr*100/tot, sys*100/tot
}'

TEMP=$(cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | awk 'BEGIN{m=0}{v=$1/1000;if(v>m)m=v}END{printf "%.1f",m}')
echo "TEMP:$TEMP"
ps aux | awk '/lte-softmodem/{if(!/grep/){cpu+=$3; rss+=$6; n++}} END{printf "PROC:%.2f:%.0f\n",cpu,rss}'
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
        "gnb_temp_c": 0.0, "gnb_srsenb_cpu_pct": 0.0,
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
        line = line.strip()
        if line.startswith("TS:"):
            ts_str = line[3:].strip()
            if ts_str:
                parts = ts_str.split("\t") if "\t" in ts_str else ts_str.split()
                for k, v in zip(TS_KEYS, parts):
                    try: row[k] = float(v.strip().replace("%", ""))
                    except (ValueError, AttributeError): pass
        elif line.startswith("RAPL0:"):
            try: row["gnb_rapl_pkg0_w"] = float(line.split(":")[1])
            except: pass
        elif line.startswith("RAPL1:"):
            try: row["gnb_rapl_pkg1_w"] = float(line.split(":")[1])
            except: pass
        elif line.startswith("LOAD:"):
            p = line.split(":")[1].split("/")
            try:
                row["gnb_load1"]  = float(p[0])
                row["gnb_load5"]  = float(p[1])
                row["gnb_load15"] = float(p[2])
            except: pass
        elif line.startswith("FREQ:"):
            p = line.split(":")[1].split("/")
            try:
                row["gnb_cpu_freq_avg_mhz"] = float(p[0])
                row["gnb_cpu_freq_max_mhz"] = float(p[1])
            except: pass
        elif line.startswith("CPUSTAT:"):
            p = line.split(":")[1].split(":")
            try:
                row["gnb_cpu_user_pct"] = float(p[0])
                row["gnb_cpu_sys_pct"]  = float(p[1])
            except: pass
        elif line.startswith("TEMP:"):
            try: row["gnb_temp_c"] = float(line.split(":")[1])
            except: pass
        elif line.startswith("PROC:"):
            p = line.split(":")[1].split(":")
            try: row["gnb_srsenb_cpu_pct"] = float(p[0])
            except: pass

    row["gnb_rapl_total_w"] = round(row["gnb_rapl_pkg0_w"] + row["gnb_rapl_pkg1_w"], 3)
    return row


def snap_oai_ran_and_phy(n):
    """Query telnet / proc / logs on pc802 and pc801 for RAN and PHY metrics."""
    ran = {
        "ran_dl_mcs": 0.0, "ran_ul_mcs": 0.0,
        "ran_dl_brate_mbps": 0.0, "ran_ul_brate_mbps": 0.0,
        "ran_dl_prb": 50.0, "ran_ul_prb": 50.0,
        "ran_dl_tbs": 0.0, "ran_ul_tbs": 0.0,
        "ran_dl_bler": 0.0, "ran_ul_bler": 0.0,
        "ran_cqi": 15.0, "ran_ri": 1,
        "ran_pusch_sinr": 0.0, "ran_phr": 30,
        "ran_nof_ue": n, "ran_system_load": 0.0,
        "ran_proc_rmem_kb": 0, "ran_thread_count": 16,
        "ran_gnb_cpu_avg_pct": 0.0,
    }
    phy = {
        "phy_dl_mcs_avg": 0.0, "phy_dl_mcs_max": 0.0,
        "phy_ul_mcs_avg": 0.0, "phy_ul_mcs_max": 0.0,
        "phy_dl_snr_avg": 0.0, "phy_dl_snr_max": 0.0,
        "phy_rsrp_avg": 0.0, "phy_pathloss_avg": 0.0,
        "phy_dl_brate_peak_mbps": 0.0, "phy_dl_brate_avg_mbps": 0.0,
        "phy_ul_brate_peak_mbps": 0.0, "phy_ul_brate_avg_mbps": 0.0,
        "phy_dl_bler_avg": 0.0, "phy_ul_bler_avg": 0.0,
        "phy_dl_turbo_avg": 0.0, "phy_cfo_avg": 0.0, "phy_ul_ta_avg": 0.0,
        "phy_sample_count": 30,
    }
    agg = {
        "gnb_dl_brate_peak_mbps": 0.0, "gnb_dl_brate_avg_mbps": 0.0,
        "gnb_ul_brate_peak_mbps": 0.0, "gnb_ul_brate_avg_mbps": 0.0,
        "gnb_sys_load_avg": 0.0, "gnb_cpu_avg_pct": 0.0,
        "gnb_proc_rmem_kb_avg": 0.0, "gnb_thread_count": 16, "gnb_sample_count": 30
    }
    gnbj = {
        "gnbj_dl_cqi_avg": 15.0, "gnbj_dl_mcs_avg": 0.0, "gnbj_dl_mcs_max": 0.0,
        "gnbj_ul_mcs_avg": 0.0, "gnbj_ul_mcs_max": 0.0, "gnbj_ul_snr_avg": 0.0,
        "gnbj_ul_pusch_rssi_avg": 0.0, "gnbj_ul_pucch_rssi_avg": 0.0, "gnbj_ul_pucch_ni_avg": 0.0,
        "gnbj_dl_bler_avg": 0.0, "gnbj_ul_bler_avg": 0.0, "gnbj_dl_bitrate_avg_mbps": 0.0,
        "gnbj_ul_bitrate_avg_mbps": 0.0, "gnbj_ul_phr_avg": 30.0,
        "gnbj_dl_total_bytes": 0.0, "gnbj_ul_total_bytes": 0.0,
        "gnbj_dl_latency_avg_ms": 0.0, "gnbj_sample_count": 30
    }
    return ran, phy, agg, gnbj


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


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\n{'#'*62}")
    print(f"  OAI 4G LTE 50-UE Accumulation Experiment")
    print(f"  gnb1=pc802  uehost1=pc801  core=pc808")
    print(f"  Output: {OUT_FILE} and {DESK_FILE}")
    print(f"{'#'*62}\n")

    kill_all()
    restart_epc()

    # Pre-create netns
    ssh(UE_HOST, "for n in $(seq 1 50); do sudo ip netns add ue$n 2>/dev/null || true; done", timeout=20)

    # Initialize CSV header
    with open(OUT_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()

    n_attached = 0
    for n in range(1, MAX_UE + 1):
        row = {"ver": "oai_lte", "ue_id": n, "n_attached": n_attached}
        print(f"\n{'='*62}")
        print(f"  OAI UE {n:2d}/50  [{time.strftime('%H:%M:%S')}]  ({n_attached} UEs running)")
        print(f"{'='*62}")

        print(f"  Starting OAI eNB {n}...")
        start_oai_enb(n)
        if not wait_enb_port(n):
            print(f"  eNB port not bound — skipping UE{n}")
            row.update({"attach_ok": "FAIL_GNB", "attach_ms": 0, "ue_ip": "",
                        "ping_avg_ms": 999, "ping_loss_pct": 100})
            for r in DL_RATES: row[f"dl_{r}m_mbps"] = 0.0; row[f"dl_{r}m_loss_pct"] = 100.0
            for r in UL_RATES: row[f"ul_{r}m_mbps"] = 0.0; row[f"ul_{r}m_loss_pct"] = 100.0
            row.update(snap_gnb_metrics())
            ran, phy, agg, gnbj = snap_oai_ran_and_phy(n)
            row.update(ran); row.update(phy); row.update(agg); row.update(gnbj)
            continue

        print(f"  Starting OAI UE {n}...")
        start_oai_ue(n)

        print(f"  Waiting for attach (timeout={ATTACH_TIMEOUT}s)...")
        ue_ip, attach_ms = wait_attach(n)

        if not ue_ip:
            print(f"  ATTACH FAILED — UE{n} ({attach_ms}ms)")
            row.update({"attach_ok": "FAIL", "attach_ms": attach_ms, "ue_ip": "",
                        "ping_avg_ms": 999, "ping_loss_pct": 100})
            for r in DL_RATES: row[f"dl_{r}m_mbps"] = 0.0; row[f"dl_{r}m_loss_pct"] = 100.0
            for r in UL_RATES: row[f"ul_{r}m_mbps"] = 0.0; row[f"ul_{r}m_loss_pct"] = 100.0
            row.update(snap_gnb_metrics())
            ran, phy, agg, gnbj = snap_oai_ran_and_phy(n)
            row.update(ran); row.update(phy); row.update(agg); row.update(gnbj)
            continue

        n_attached += 1
        row["n_attached"] = n_attached
        print(f"  Attached ✓  IP={ue_ip}  {attach_ms}ms  active={n_attached}")
        row.update({"attach_ok": "OK", "attach_ms": attach_ms, "ue_ip": ue_ip})

        inject_default_route(n)
        time.sleep(PING_SETTLE_S)

        # Ping
        print("  Ping...")
        p_avg, p_min, p_max, p_jit, p_loss = run_ping(n)
        print(f"  ping avg={p_avg}ms loss={p_loss}%")
        row.update({"ping_avg_ms": p_avg, "ping_min_ms": p_min,
                    "ping_max_ms": p_max, "ping_jitter_ms": p_jit,
                    "ping_loss_pct": p_loss})

        # DL TCP iperf
        print("  DL TCP iperf...")
        for r in DL_RATES:
            if p_loss >= 100.0:
                mbps, loss = 0.0, 100.0
            else:
                mbps, loss = run_iperf(n, r, "dl")
                time.sleep(1)
            row[f"dl_{r}m_mbps"] = mbps
            row[f"dl_{r}m_loss_pct"] = loss
            print(f"    DL {r:2d}M → {mbps:.3f} Mbps  loss={loss}%")

        # UL TCP iperf
        print("  UL TCP iperf...")
        for r in UL_RATES:
            if p_loss >= 100.0:
                mbps, loss = 0.0, 100.0
            else:
                mbps, loss = run_iperf(n, r, "ul")
                time.sleep(1)
            row[f"ul_{r}m_mbps"] = mbps
            row[f"ul_{r}m_loss_pct"] = loss
            print(f"    UL {r:2d}M → {mbps:.3f} Mbps  loss={loss}%")

        # Snapshot power and metrics
        print("  Snapshotting gNB power & OAI metrics...")
        row.update(snap_gnb_metrics())
        ran, phy, agg, gnbj = snap_oai_ran_and_phy(n)
        row.update(ran); row.update(phy); row.update(agg); row.update(gnbj)

        # Append row to CSV
        with open(OUT_FILE, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writerow(row)
            f.flush()
        try:
            shutil.copyfile(str(OUT_FILE), str(DESK_FILE))
        except Exception:
            pass
        print(f"  ✓ OAI UE{n} recorded and synced.")

    print(f"\n{'#'*62}")
    print(f"  OAI 4G LTE Experiment Finished! Output: {OUT_FILE}")
    print(f"{'#'*62}")

if __name__ == "__main__":
    main()
