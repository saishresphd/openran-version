#!/usr/bin/env python3
"""
rerun_v23_failed_and_udp_600s.py — Fast-attach valid UEs and run full TCP measurements for targeted UEs with 600s timeout.
==========================================================================================================================
Target UEs to measure with full 170-metric suite (standard TCP):
  [30, 36, 38, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50]
Fast-attach all remaining UEs (1..29, 31..35, 37, 39) keeping their valid TCP data.

Execution:
  - Clean slate: kills leftover processes, restarts EPC, cleans contexts/tuns, setups GTP aliases.
  - Phase 1 & 2 loop for UEs 1..50:
      - If UE in TARGET_MEASURE_UES: starts gNB & UE, waits attach (up to 600s), default route injection,
        runs ping + 18-rate DL TCP iperf + 18-rate UL TCP iperf + full turbostat / RAPL / RAN / PHY / gNB JSON snapshot.
      - Else (valid previous UE): starts gNB & UE, waits attach, injects route, retains existing valid data.
  - Syncs after every UE to:
      - results/ver_eval/v23_11/ue_results_50.csv
      - ~/Desktop/v23_11_ue_results_50.csv
"""

import subprocess, time, csv, os, re, json, pathlib, hashlib, shutil, signal, threading

# ── Node config ───────────────────────────────────────────────────────────────
KEY      = os.path.expanduser("~/.ssh/id_ed25519")
GNB_HOST = "saish@pc802.emulab.net"
UE_HOST  = "saish@pc801.emulab.net"
CORE_HOST= "saish@pc808.emulab.net"

SRSENB_BIN   = "/opt/srsRAN_v23/build/srsenb/src/srsenb"
SRSUE_BIN    = "/opt/srsRAN_v23/build/srsue/src/srsue"
ENB_CONF_DIR = "/etc/srsenb_v23"
UE_CONF_DIR  = "/etc/srsue_v23"
UE_LOG_DIR   = "/tmp/ue_logs_v23"
GNB_LOG_DIR  = "/tmp/gnb_logs_v23"

GNB_GTP_BASE = "10.10.2"
GNB_LAN_IF   = "enp4s0f1"

ATTACH_TIMEOUT      = 600   # 600 seconds attach timeout
FAST_ATTACH_TIMEOUT = 120
PING_COUNT          = 20
PING_SETTLE_S       = 5
DL_RATES = list(range(1, 11)) + list(range(15, 51, 5))   # 18 rates
UL_RATES = list(range(1, 11)) + list(range(15, 51, 5))   # 18 rates
IPERF_DUR           = 10
MAX_UE              = 50
CORE_IP             = "10.45.0.1"

# Target UEs to measure with full TCP tests:
# User specified: UE30,36,38,40,41,42,43,44,45,46,47,48,49,50
TARGET_MEASURE_UES = set([30, 36, 38, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50])

OUT_DIR   = pathlib.Path("results/ver_eval/v23_11")
OUT_FILE  = OUT_DIR / "ue_results_50.csv"
DESK_FILE = pathlib.Path(os.path.expanduser("~/Desktop/v23_11_ue_results_50.csv"))

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
    print("  Killing srsenb (pc802) & srsue (pc801)...")
    ssh(GNB_HOST, "sudo killall srsenb 2>/dev/null; sleep 1; "
                  "sudo killall -9 srsenb 2>/dev/null; sleep 1; echo done", timeout=15)
    ssh(UE_HOST,  "sudo killall srsue  2>/dev/null; sleep 4; "
                  "sudo killall -9 srsue 2>/dev/null; sleep 2; echo done", timeout=15)
    time.sleep(5)


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


def inject_default_route(n):
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
    print(f"  Setting up GTP alias IPs 10.10.2.1-{max_n} on pc802 (enp4s0f1)...")
    alias_script = f"for n in $(seq 1 {max_n}); do sudo ip addr add {GNB_GTP_BASE}.$n/24 dev {GNB_LAN_IF} 2>/dev/null || true; done; echo count=$(ip addr show {GNB_LAN_IF} | grep -c '{GNB_GTP_BASE}\\.' || echo 0)"
    out, _ = ssh(GNB_HOST, alias_script, timeout=30)
    print(f"  {out.strip()}")

    print(f"  Patching enb_ue*.conf: gtp_bind_addr → 10.10.2.n ...")
    patch_script = (
        f"for n in $(seq 1 {max_n}); do "
        f"  sudo sed -i \"s/^gtp_bind_addr.*/gtp_bind_addr = {GNB_GTP_BASE}.$n/\" "
        f"  {ENB_CONF_DIR}/enb_ue${{n}}.conf 2>/dev/null; "
        f"  sudo sed -i \"s/^s1c_bind_addr.*/s1c_bind_addr = {GNB_GTP_BASE}.$n/\" "
        f"  {ENB_CONF_DIR}/enb_ue${{n}}.conf 2>/dev/null; "
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
    print(f"  [HEAL] 100% ping loss on UE{ue_n} — starting full re-attach cycle...")
    print(f"  [HEAL] Step 1/4: killing srsue{ue_n} + srsenb{ue_n}...")
    ssh(UE_HOST,
        f"sudo pkill -9 -f 'srsue.*ue{ue_n}\\.conf' 2>/dev/null; sleep 1; echo done",
        timeout=12)
    ssh(GNB_HOST,
        f"sudo pkill -9 -f 'srsenb.*enb_ue{ue_n}\\.conf' 2>/dev/null; sleep 1; echo done",
        timeout=12)
    time.sleep(3)

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

    print(f"  [HEAL] Step 3/4: restarting gNB slot {ue_n}...")
    start_gnb_ue(ue_n)
    if not wait_gnb_port(ue_n, timeout=25):
        print(f"  [HEAL] gNB port not bound — heal failed")
        return False, "", 0

    print(f"  [HEAL] Step 4/4: re-attaching srsue{ue_n} (timeout={ATTACH_TIMEOUT}s)...")
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
    inject_default_route(ue_n)
    time.sleep(PING_SETTLE_S)

    p_avg, _, _, _, p_loss = run_ping(ue_n, count=5)
    print(f"  [HEAL] Post-heal ping: avg={p_avg}ms loss={p_loss}%")
    if p_loss < 50.0:
        print(f"  [HEAL] ✓ RECOVERED")
        return True, new_ip, new_ms
    print(f"  [HEAL] Ping still failing after re-attach — heal failed")
    return False, new_ip, new_ms


def ensure_iperf_server(n):
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
    """Run standard TCP iperf3 from UE n netns. Returns (mbps, loss_pct)."""
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


def snap_gnb_metrics():
    script = r"""
TS_COLS="Avg_MHz,Busy%,Bzy_MHz,IPC,IRQ,SMI,CPU%c1,CPU%c3,CPU%c6,CPU%c7,CoreTmp,PkgTmp,Pkg%pc2,Pkg%pc3,PkgWatt,RAMWatt,PKG_%,RAM_%"
TSFILE=$(mktemp /tmp/ts_XXXXXX)
sudo turbostat --quiet --show $TS_COLS sleep 5 > $TSFILE 2>&1
TS_OUT=$(grep -v '^$' "$TSFILE" \
  | grep -v '^[A-Za-z]' \
  | grep -v '^[0-9][0-9.]*[[:space:]]*sec' \
  | awk -F'\t' 'NF>=15{print; exit}')
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

awk 'BEGIN{s=0;n=0;mx=0} /cpu MHz/{v=$4+0; s+=v; n++; if(v>mx)mx=v}
     END{if(n>0)printf "FREQ:%.0f/%.0f\n",s/n,mx}' /proc/cpuinfo

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
                parts = ts_str.split("\t")
                if len(parts) == 1:
                    parts = ts_str.split()
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


def snap_gnb_ran(n):
    script = f"""
LOG="{GNB_LOG_DIR}/enb_ue{n}.log"
RPT="{GNB_LOG_DIR}/enb_ue{n}_report.json"
MET="{GNB_LOG_DIR}/enb_ue{n}_metrics.csv"

echo "===LOG_TAIL==="
if [ -f "$LOG" ]; then tail -30 "$LOG"; fi
echo "===RPT_TAIL==="
if [ -f "$RPT" ]; then tail -c 100000 "$RPT"; fi
echo "===MET_TAIL==="
if [ -f "$MET" ]; then tail -5 "$MET"; fi

ps aux | awk -v u="enb_ue{n}.conf" '$0~u && !/awk/{{print "PROC_INFO:"$3":"$6}}'
"""
    out, _ = ssh(GNB_HOST, script, timeout=15)
    ran = {
        "ran_dl_mcs": 0.0, "ran_ul_mcs": 0.0,
        "ran_dl_brate_mbps": 0.0, "ran_ul_brate_mbps": 0.0,
        "ran_dl_prb": 0.0, "ran_ul_prb": 0.0,
        "ran_dl_tbs": 0.0, "ran_ul_tbs": 0.0,
        "ran_dl_bler": 0.0, "ran_ul_bler": 0.0,
        "ran_cqi": 15.0, "ran_ri": 0,
        "ran_pusch_sinr": 0.0, "ran_phr": 30,
        "ran_nof_ue": 1, "ran_system_load": 0.0,
        "ran_proc_rmem_kb": 0, "ran_thread_count": 14,
        "ran_gnb_cpu_avg_pct": 0.0,
    }

    sections = {}
    cur_sec  = "HEADER"
    sec_lines = []
    for line in out.splitlines():
        if line.startswith("===") and line.endswith("==="):
            sections[cur_sec] = sec_lines
            cur_sec   = line.strip("=").strip()
            sec_lines = []
        else:
            sec_lines.append(line)
    sections[cur_sec] = sec_lines

    proc_lines = [l for l in sections.get("HEADER", []) if l.startswith("PROC_INFO:")]
    if proc_lines:
        p = proc_lines[-1].split(":")[1:]
        try:
            ran["ran_gnb_cpu_avg_pct"] = float(p[0])
            ran["ran_proc_rmem_kb"]    = int(p[1])
        except: pass

    met_lines = [l.strip() for l in sections.get("MET_TAIL", []) if l.strip()]
    if len(met_lines) >= 2:
        hdr = [h.strip() for h in met_lines[0].split(";")]
        row_vals = [v.strip() for v in met_lines[-1].split(";")]
        m = dict(zip(hdr, row_vals))
        def mf(k, d=0.0):
            try: return float(m.get(k, d) or d)
            except: return d
        ran["ran_dl_mcs"]       = mf("dl_mcs",       ran["ran_dl_mcs"])
        ran["ran_ul_mcs"]       = mf("ul_mcs",       ran["ran_ul_mcs"])
        ran["ran_dl_prb"]       = mf("dl_prb",       ran["ran_dl_prb"])
        ran["ran_ul_prb"]       = mf("ul_prb",       ran["ran_ul_prb"])
        ran["ran_dl_bler"]      = mf("dl_bler",      ran["ran_dl_bler"])
        ran["ran_ul_bler"]      = mf("ul_bler",      ran["ran_ul_bler"])
        ran["ran_cqi"]          = mf("cqi",          ran["ran_cqi"])
        ran["ran_pusch_sinr"]   = mf("pusch_sinr",   ran["ran_pusch_sinr"])
        ran["ran_phr"]          = int(mf("phr",      ran["ran_phr"]))
        ran["ran_nof_ue"]       = int(mf("nof_ue",   ran["ran_nof_ue"]))
        ran["ran_system_load"]  = mf("system_load",  ran["ran_system_load"])
        ran["ran_proc_rmem_kb"] = int(mf("proc_rmem_kB", ran["ran_proc_rmem_kb"]))
        ran["ran_thread_count"] = int(mf("thread_count", ran["ran_thread_count"]))
        dl_b = mf("dl_brate", 0.0)
        ul_b = mf("ul_brate", 0.0)
        if dl_b > 0: ran["ran_dl_brate_mbps"] = round(dl_b / 1e6, 4)
        if ul_b > 0: ran["ran_ul_brate_mbps"] = round(ul_b / 1e6, 4)

    rpt_raw = "\n".join(sections.get("RPT_TAIL", []))
    last_ue_c   = None
    last_bearer = None
    dec = json.JSONDecoder()
    i = 0
    while i < len(rpt_raw):
        try:
            obj, idx = dec.raw_decode(rpt_raw, i)
            i = idx
            if isinstance(obj, dict) and obj.get("type") == "metrics":
                for cell in obj.get("cell_list", []):
                    cc = cell.get("cell_container", {})
                    for ue in cc.get("ue_list", []):
                        last_ue_c = ue.get("ue_container", {})
                        for b in last_ue_c.get("bearer_list", []):
                            bc = b.get("bearer_container", {})
                            if bc.get("qci") == 9:
                                last_bearer = bc
        except json.JSONDecodeError:
            i += 1

    if last_ue_c:
        def uf(k, d=0.0):
            try: return float(last_ue_c.get(k, d) or d)
            except: return d
        cqi = uf("dl_cqi", 0)
        if cqi > 0: ran["ran_cqi"] = cqi
        sinr = uf("ul_sinr", 0) or uf("ul_pusch_snr", 0)
        if sinr != 0: ran["ran_pusch_sinr"] = sinr
        phr = uf("ul_phr", 0)
        if phr != 0: ran["ran_phr"] = int(phr)
        if uf("dl_mcs") > 0: ran["ran_dl_mcs"] = uf("dl_mcs")
        if uf("ul_mcs") > 0: ran["ran_ul_mcs"] = uf("ul_mcs")
        ran["ran_dl_bler"] = uf("dl_bler")
        ran["ran_ul_bler"] = uf("ul_bler")
        if last_bearer:
            dl_b = float(last_bearer.get("dl_total_bytes", 0) or 0)
            ul_b = float(last_bearer.get("ul_total_bytes", 0) or 0)
            if dl_b > 0: ran["ran_dl_brate_mbps"] = round(dl_b / 1e6, 4)
            if ul_b > 0: ran["ran_ul_brate_mbps"] = round(ul_b / 1e6, 4)

    return ran


def snap_ue_phy(n):
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
    phy["phy_dl_turbo_avg"] = safe_avg(col_vals("turbo_iters", "dl_turbo"))
    phy["phy_cfo_avg"]      = safe_avg(col_vals("cfo", "dl_cfo"))
    phy["phy_ul_ta_avg"]    = safe_avg(col_vals("ta", "ul_ta"))
    phy["phy_sample_count"] = len(rows)
    return phy


def snap_gnb_agg(n):
    script = f"""
MET="{GNB_LOG_DIR}/enb_ue{n}_metrics.csv"
if [ -f "$MET" ]; then
  echo "GNB_HDR:$(head -1 $MET)"
  tail -30 "$MET" | grep -v '^$' | while IFS= read -r line; do echo "GNB_ROW:$line"; done
fi
"""
    out, _ = ssh(GNB_HOST, script, timeout=15)
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
        if line.startswith("GNB_HDR:"):
            hdr = line[8:].split(";")
        elif line.startswith("GNB_ROW:"):
            rows.append(line[8:].split(";"))

    if not hdr or not rows:
        return agg

    def idx_of(*keys):
        for key in keys:
            for i, h in enumerate(hdr):
                if h.strip() == key:
                    return i
        return None

    def col_vals(*keys):
        for key in keys:
            i = idx_of(key)
            if i is None: continue
            vals = []
            for r in rows:
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

    dl_b = [v / 1e6 for v in col_vals("dl_brate") if v > 0]
    agg["gnb_dl_brate_peak_mbps"] = safe_max(dl_b)
    agg["gnb_dl_brate_avg_mbps"]  = safe_avg(dl_b)

    ul_b = [v / 1e6 for v in col_vals("ul_brate") if v > 0]
    agg["gnb_ul_brate_peak_mbps"] = safe_max(ul_b)
    agg["gnb_ul_brate_avg_mbps"]  = safe_avg(ul_b)

    agg["gnb_sys_load_avg"] = safe_avg(col_vals("sys_load", "system_load"))

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

    rmem = col_vals("proc_rmem_kB", "proc_rmem_kb", "rmem_kb")
    agg["gnb_proc_rmem_kb_avg"] = safe_avg(rmem)

    thr = col_vals("thread_count", "nof_threads")
    agg["gnb_thread_count"] = int(safe_avg(thr))
    agg["gnb_sample_count"] = len(rows)
    return agg


def snap_gnbj(n):
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

        brate_dl = fv("dl_bitrate")
        brate_ul = fv("ul_bitrate")
        has_traffic = brate_dl > 0 or brate_ul > 0

        acc["gnbj_dl_cqi_avg"].append(fv("dl_cqi"))
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

        for b in u.get("bearer_list", []):
            bc = b.get("bearer_container", {})
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


def run_single_ue_measurements(n, ue_ip, attach_ms):
    row = {"ver": "v23_11", "ue_id": n, "attach_ok": "OK", "attach_ms": attach_ms, "ue_ip": ue_ip}

    # Ping
    print("  Ping...")
    p_avg, p_min, p_max, p_jit, p_loss = run_ping(n)
    print(f"  ping avg={p_avg}ms loss={p_loss}%")

    if p_loss >= 100.0:
        healed, new_ip, new_ms = heal_upf(n)
        if healed:
            if new_ip:
                ue_ip = new_ip
                attach_ms = new_ms
                row.update({"ue_ip": ue_ip, "attach_ms": attach_ms})
            p_avg, p_min, p_max, p_jit, p_loss = run_ping(n)
            print(f"  ping avg={p_avg}ms loss={p_loss}%")
        else:
            print(f"  [WARN] UPF heal failed UE{n} — writing row with loss=100, iperf skipped")

    row.update({"ping_avg_ms": p_avg, "ping_min_ms": p_min,
                 "ping_max_ms": p_max, "ping_jitter_ms": p_jit,
                 "ping_loss_pct": p_loss})

    # DL TCP iperf — 18 rates
    print("  DL TCP iperf...")
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

    # UL TCP iperf — 18 rates
    print("  UL TCP iperf...")
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

    # Snapshots
    print("  gNB RAN params...")
    ran = ran_bg if ran_bg else snap_gnb_ran(n)
    print(f"  dl_mcs={ran['ran_dl_mcs']}  ul_mcs={ran['ran_ul_mcs']}"
          f"  cqi={ran['ran_cqi']}  sinr={ran['ran_pusch_sinr']}dB")
    row.update(ran)

    print("  gNB power snapshot (turbostat)...")
    snap = snap_gnb_metrics()
    print(f"  pkg={snap['gnb_ts_pkg_watt']}W(ts)  rapl={snap['gnb_rapl_total_w']}W  "
          f"busy={snap['gnb_ts_busy_pct']}%  load={snap['gnb_load1']}/{snap['gnb_load5']}  "
          f"CoreTmp={snap['gnb_ts_core_tmp']}°C")
    row.update(snap)

    agg = agg_bg if agg_bg else snap_gnb_agg(n)
    print(f"  gnb_agg: dl_peak={agg['gnb_dl_brate_peak_mbps']}Mbps  n_samples={agg['gnb_sample_count']}")
    row.update(agg)

    gj = gnbj_bg if gnbj_bg else snap_gnbj(n)
    print(f"  gnbj: cqi={gj['gnbj_dl_cqi_avg']}  dl_mcs={gj['gnbj_dl_mcs_avg']}  n_samples={gj['gnbj_sample_count']}")
    row.update(gj)

    phy = phy_bg if phy_bg else snap_ue_phy(n)
    print(f"  phy: dl_mcs_avg={phy['phy_dl_mcs_avg']}  dl_snr_avg={phy['phy_dl_snr_avg']}  rsrp={phy['phy_rsrp_avg']}")
    row.update(phy)

    return row


def main():
    print(f"\n{'#'*62}")
    print(f"  v23.11  Rerun Targeted UEs {sorted(list(TARGET_MEASURE_UES))} with TCP & 600s Timeout")
    print(f"  gnb1=pc802  uehost1=pc801  core=pc808")
    print(f"  Output: {OUT_FILE} and {DESK_FILE}")
    print(f"{'#'*62}\n")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    good_dict = {}
    if OUT_FILE.exists() and OUT_FILE.stat().st_size > 0:
        with open(OUT_FILE, newline="") as fh:
            reader = csv.DictReader(fh)
            for r in reader:
                ue = int(r.get("ue_id", 0))
                # Only retain if not in TARGET_MEASURE_UES and attach_ok == 'OK'
                if ue not in TARGET_MEASURE_UES:
                    try: ploss = float(r.get("ping_loss_pct", 100))
                    except ValueError: ploss = 100.0
                    if r.get("attach_ok", "") == "OK" and ploss < 100.0:
                        good_dict[ue] = r

    print(f"  Retaining {len(good_dict)} clean valid TCP rows from previous run: {sorted(list(good_dict.keys()))}")
    print(f"  Target UEs to measure fresh: {sorted(list(TARGET_MEASURE_UES))}")

    # Clean slate on testbed
    kill_all()
    restart_epc()

    ssh(GNB_HOST, f"mkdir -p {GNB_LOG_DIR}", timeout=10)
    ssh(UE_HOST,  f"mkdir -p {UE_LOG_DIR}",  timeout=10)
    setup_gtp_aliases(MAX_UE)
    precreate_netns(MAX_UE)

    print("Starting persistent iperf3 server loops on core (ports 5201-5250)...")
    ssh(CORE_HOST, "sudo pkill -9 iperf3 2>/dev/null; sleep 1; echo done", timeout=10)
    time.sleep(2)
    for n in range(1, MAX_UE + 1):
        port = 5200 + n
        loop = (f"while true; do "
                f"iperf3 -s -B {CORE_IP} -p {port} --one-off 2>/dev/null; "
                f"sleep 0.2; done")
        ssh_bg(CORE_HOST, loop)
    time.sleep(6)
    bound, _ = ssh(CORE_HOST, "ss -tnlp | grep -cE ':52[0-9]{2} ' || echo 0", timeout=8)
    print(f"  iperf3 servers ready: {bound.strip()} ports listening")

    final_rows = {}
    n_attached = 0

    def sync_to_disk():
        sorted_keys = sorted(final_rows.keys())
        with open(OUT_FILE, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            for k in sorted_keys:
                w.writerow(final_rows[k])
            f.flush()
            os.fsync(f.fileno())
        try:
            shutil.copyfile(str(OUT_FILE), str(DESK_FILE))
        except Exception:
            pass

    for n in range(1, MAX_UE + 1):
        is_target_ue = n in TARGET_MEASURE_UES
        cur_timeout  = ATTACH_TIMEOUT if is_target_ue else FAST_ATTACH_TIMEOUT

        print(f"\n{'='*62}")
        print(f"  UE {n:2d}/50  [{time.strftime('%H:%M:%S')}] — {'[FULL TCP MEASUREMENT (600s TIMEOUT)]' if is_target_ue else '[FAST ATTACH]'}")
        print(f"  Active UEs running: {n_attached}")
        print(f"{'='*62}")

        print(f"  Starting srsenb {n}...")
        start_gnb_ue(n)
        if not wait_gnb_port(n, timeout=25):
            print(f"  PORT_FAIL — srsenb {n} did not bind")
            row = {"ver": "v23_11", "ue_id": n, "n_attached": n_attached, "attach_ok": "FAIL_GNB", "attach_ms": 0, "ue_ip": ""}
            for fld in FIELDS: row.setdefault(fld, 0.0 if "mbps" in fld or "pct" in fld else "")
            final_rows[n] = row
            sync_to_disk()
            continue

        print(f"  Starting srsue {n}...")
        start_srsue(n)
        time.sleep(2)

        print(f"  Waiting for attach (timeout={cur_timeout}s)...")
        ue_ip, attach_ms = wait_attach(n, timeout=cur_timeout)

        if not ue_ip:
            print(f"  ATTACH FAILED — UE{n} ({attach_ms}ms)")
            row = {"ver": "v23_11", "ue_id": n, "n_attached": n_attached, "attach_ok": "FAIL", "attach_ms": attach_ms, "ue_ip": "", "ping_avg_ms": 999.0, "ping_loss_pct": 100.0}
            for fld in FIELDS: row.setdefault(fld, 0.0 if "mbps" in fld or "loss" in fld else "")
            final_rows[n] = row
            sync_to_disk()
            continue

        n_attached += 1
        print(f"  Attached ✓  IP={ue_ip}  {attach_ms}ms  active={n_attached}")
        inject_default_route(n)
        time.sleep(PING_SETTLE_S)

        if is_target_ue:
            measured_row = run_single_ue_measurements(n, ue_ip, attach_ms)
            measured_row["n_attached"] = n_attached
            for fld in FIELDS: measured_row.setdefault(fld, "")
            final_rows[n] = measured_row
            sync_to_disk()
            print(f"  ✓ UE{n} fully measured & saved to CSV/Desktop.")
        else:
            row = dict(good_dict[n])
            row["n_attached"] = n_attached
            row["attach_ms"] = attach_ms
            row["ue_ip"] = ue_ip
            final_rows[n] = row
            sync_to_disk()
            print(f"  ✓ UE{n} fast-attached & retained valid TCP data.")

    print(f"\n{'#'*62}")
    print(f"  v23.11 Experiment Complete! Rows: {len(final_rows)}/50")
    print(f"  Output: {OUT_FILE} and {DESK_FILE}")
    print(f"{'#'*62}")

if __name__ == "__main__":
    main()
