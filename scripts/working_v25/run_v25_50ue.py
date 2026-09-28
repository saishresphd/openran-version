#!/usr/bin/env python3
"""
run_v25_50ue.py — srsRAN 5G (v25) / Project Version 50-UE experiment suite.
===========================================================================
Node mapping:
  core    = pc808   10.10.1.1   Open5GS 5G Core / EPC
  gnb1    = pc802   10.10.1.2   gnb process per UE
  uehost1 = pc801   10.10.1.4   ue process per UE

Output: results/ver_eval/v25_10/ue_results_50.csv — 170-column full schema
"""

import subprocess, time, csv, os, re, json, pathlib, hashlib, shutil, signal, threading

# ── Node config ───────────────────────────────────────────────────────────────
KEY      = os.path.expanduser("~/.ssh/id_ed25519")
GNB_HOST = "saish@pc802.emulab.net"
UE_HOST  = "saish@pc801.emulab.net"
CORE_HOST= "saish@pc808.emulab.net"

SRSENB_BIN   = "/opt/srsRAN_v25/build/srsenb/src/srsenb"
SRSUE_BIN    = "/opt/srsRAN_v25/build/srsue/src/srsue"
ENB_CONF_DIR = "/etc/srsenb_v25"
UE_CONF_DIR  = "/etc/srsue_v25"
UE_LOG_DIR   = "/tmp/ue_logs_v25"
GNB_LOG_DIR  = "/tmp/gnb_logs_v25"

GNB_GTP_BASE = "10.10.2"
GNB_LAN_IF   = "enp4s0f1"

ATTACH_TIMEOUT = 75
PING_COUNT     = 20
PING_SETTLE_S  = 5
DL_RATES = list(range(1, 11)) + list(range(15, 51, 5))
UL_RATES = list(range(1, 11)) + list(range(15, 51, 5))
IPERF_DUR      = 10
MAX_UE         = 50
MEASURE_FROM   = 1
CORE_IP        = "10.45.0.1"

OUT_DIR  = pathlib.Path("results/ver_eval/v25_10")
OUT_FILE = OUT_DIR / "ue_results_50.csv"

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
                   capture_output=True, text=True, timeout=12)
    time.sleep(0.5)


def kill_all():
    ssh(GNB_HOST, "sudo killall srsenb 2>/dev/null; sleep 1; "
                  "sudo killall -9 srsenb 2>/dev/null; sleep 1; echo done", timeout=15)
    ssh(UE_HOST,  "sudo killall srsue  2>/dev/null; sleep 4; "
                  "sudo killall -9 srsue  2>/dev/null; sleep 1; echo done", timeout=15)
    time.sleep(5)


def restart_epc():
    print("  Restarting Core...")
    script = """
sudo systemctl restart open5gs-smfd   && sleep 3
sudo systemctl restart open5gs-upfd   && sleep 2
sudo systemctl restart open5gs-sgwud  && sleep 2
sudo systemctl restart open5gs-sgwcd  && sleep 2
sudo systemctl restart open5gs-mmed   && sleep 4
systemctl is-active open5gs-mmed open5gs-smfd open5gs-sgwcd open5gs-sgwud open5gs-upfd
"""
    out, _ = ssh(CORE_HOST, script, timeout=30)
    statuses = [l.strip() for l in out.splitlines() if l.strip() in ("active", "inactive", "failed")]
    all_active = all(s == "active" for s in statuses)
    print(f"  Core restart {'OK' if all_active else 'WARN'}: {statuses}")
    return all_active


def start_gnb_ue(n):
    conf    = f"{ENB_CONF_DIR}/enb_ue{n}.conf"
    log     = f"{GNB_LOG_DIR}/enb_ue{n}.log"
    met_csv = f"{GNB_LOG_DIR}/enb_ue{n}_metrics.csv"
    rpt_j   = f"{GNB_LOG_DIR}/enb_ue{n}_report.json"
    cmd = (f"mkdir -p {GNB_LOG_DIR} && "
           f"nohup sudo {SRSENB_BIN} {conf} "
           f"--expert.metrics_csv_filename={met_csv} "
           f"--expert.metrics_json_filename={rpt_j} "
           f"> {log} 2>&1 &")
    ssh(GNB_HOST, cmd, timeout=10)


def start_srsue(n):
    conf    = f"{UE_CONF_DIR}/ue{n}.conf"
    log     = f"{UE_LOG_DIR}/ue{n}.log"
    met_csv = f"{UE_LOG_DIR}/ue{n}_metrics.csv"
    cmd = (f"mkdir -p {UE_LOG_DIR} /tmp/ue_ctx/ue{n} && "
           f"sudo ip netns add ue{n} 2>/dev/null || true; "
           f"nohup sudo ip netns exec ue{n} {SRSUE_BIN} {conf} "
           f"--general.metrics_csv_filename={met_csv} "
           f"--nas.ctx_dir=/tmp/ue_ctx/ue{n} "
           f"> {log} 2>&1 &")
    ssh(UE_HOST, cmd, timeout=10)


def wait_gnb_port(n, timeout=25):
    port = 40000 + n * 10
    t0 = time.time()
    while time.time() - t0 < timeout:
        out, _ = ssh(GNB_HOST, f"ss -unlp | grep ':{port} ' || true", timeout=5)
        if str(port) in out:
            return True
        time.sleep(1)
    return False


def wait_attach(n, timeout=ATTACH_TIMEOUT):
    t0 = time.time()
    while time.time() - t0 < timeout:
        out, _ = ssh(UE_HOST,
                     f"sudo ip netns exec ue{n} ip -br a 2>/dev/null "
                     f"| grep tun | awk '{{print $3}}'",
                     timeout=8)
        ip = out.strip().split("/")[0] if out.strip() else ""
        if ip.startswith("10.45."):
            return ip, int((time.time() - t0) * 1000)
        time.sleep(2)
    return None, int((time.time() - t0) * 1000)


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
    alias_script = f"for n in $(seq 1 {max_n}); do sudo ip addr add {GNB_GTP_BASE}.$n/24 dev {GNB_LAN_IF} 2>/dev/null || true; done; echo count=$(ip addr show {GNB_LAN_IF} | grep -c '{GNB_GTP_BASE}\.' || echo 0)"
    ssh(GNB_HOST, alias_script, timeout=30)
    patch_script = (
        f"for n in $(seq 1 {max_n}); do "
        f"  sudo sed -i \"s/^gtp_bind_addr.*/gtp_bind_addr = {GNB_GTP_BASE}.$n/\" "
        f"  {ENB_CONF_DIR}/enb_ue${{n}}.conf 2>/dev/null; "
        f"done; "
        f"echo patched"
    )
    ssh(GNB_HOST, patch_script, timeout=30)
    route_script = (
        f"sudo ip route add {GNB_GTP_BASE}.0/24 via 10.10.1.2 dev enp4s0f1 2>/dev/null || "
        f"sudo ip route change {GNB_GTP_BASE}.0/24 via 10.10.1.2 dev enp4s0f1 2>/dev/null || true; "
        f"echo route=$(ip route show | grep '{GNB_GTP_BASE}' | head -1)"
    )
    ssh(CORE_HOST, route_script, timeout=15)


def run_ping(n):
    cmd = f"sudo ip netns exec ue{n} ping -c {PING_COUNT} -i 0.2 -W 1 {CORE_IP}"
    out, _ = ssh(UE_HOST, cmd, timeout=15)
    p_loss = 100.0; p_avg = 999.0; p_min = 999.0; p_max = 999.0; p_jit = 999.0
    for line in out.splitlines():
        if "packet loss" in line:
            m = re.search(r"(\d+(?:\.\d+)?)%\s*packet loss", line)
            if m: p_loss = float(m.group(1))
        if line.startswith("rtt min/avg/max/mdev") or line.startswith("round-trip min/avg/max/stddev"):
            parts = line.split("=")[1].strip().split()[0].split("/")
            try:
                p_min = float(parts[0]); p_avg = float(parts[1])
                p_max = float(parts[2]); p_jit = float(parts[3])
            except (IndexError, ValueError): pass
    return p_avg, p_min, p_max, p_jit, p_loss


def heal_upf(n):
    script = """
sudo systemctl restart open5gs-upfd   && sleep 1
sudo systemctl restart open5gs-sgwud  && sleep 1
sudo systemctl restart open5gs-sgwcd  && sleep 1
sudo systemctl restart open5gs-smfd   && sleep 2
"""
    ssh(CORE_HOST, script, timeout=20)
    ssh(UE_HOST, f"sudo pkill -f 'srsue {UE_CONF_DIR}/ue{n}.conf' 2>/dev/null; sleep 2", timeout=10)
    start_srsue(n)
    time.sleep(2)
    new_ip, new_ms = wait_attach(n)
    if new_ip:
        inject_default_route(n)
        time.sleep(3)
        _, _, _, _, p_loss = run_ping(n)
        if p_loss < 100.0:
            return True, new_ip, new_ms
    return False, None, 0


def run_iperf(n, rate_mbps, direction="dl"):
    port = 5200 + n
    dur  = IPERF_DUR
    if direction == "dl":
        cmd = (f"sudo ip netns exec ue{n} "
               f"iperf3 -c {CORE_IP} -u -b {rate_mbps}M -t {dur} -p {port} -R -J 2>/dev/null")
    else:
        cmd = (f"sudo ip netns exec ue{n} "
               f"iperf3 -c {CORE_IP} -u -b {rate_mbps}M -t {dur} -p {port} -J 2>/dev/null")
    out, _ = ssh(UE_HOST, cmd, timeout=dur + 15)
    try:
        data = json.loads(out)
        end  = data.get("end", {})
        if "sum" in end: s = end["sum"]
        elif "sum_received" in end: s = end["sum_received"]
        else: s = end.get("sum_sent", {})
        mbps = round(s.get("bits_per_second", 0) / 1e6, 3)
        loss = round(s.get("lost_percent", 0.0), 2)
        return mbps, loss
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
        "gnb_temp_c": 0.0, "gnb_srsenb_cpu_pct": 0.0,
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
                row["gnb_cpu_freq_mhz"]     = float(parts[0])
                if len(parts) > 1: row["gnb_cpu_freq_max_mhz"] = float(parts[1])
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


def snap_gnb_ran(n):
    script = f"""
MET="{GNB_LOG_DIR}/enb_ue{n}_metrics.csv"
RPT="{GNB_LOG_DIR}/enb_ue{n}_report.json"
if [ -f "$MET" ]; then
  echo "METRICS_HDR:$(head -1 $MET)"
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
            hdr = line[12:].split(";")
        elif line.startswith("METRICS_LAST:"):
            last = line[13:].split(";")
        else:
            json_lines.append(line)

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
        cpu_vals = [g(k) for k in row_map if k.startswith("cpu_") and row_map[k]]
        if cpu_vals:
            ran["ran_gnb_cpu_avg_pct"] = round(sum(cpu_vals) / len(cpu_vals), 2)

    json_text = "\n".join(json_lines)
    dec = json.JSONDecoder()
    last_traffic_u = None; last_u = None; last_bearer = None
    i = 0
    while i < len(json_text):
        try:
            obj, idx = dec.raw_decode(json_text, i)
            i = idx
        except json.JSONDecodeError:
            i += 1; continue
        if not isinstance(obj, dict) or obj.get("type") != "metrics": continue
        for cell in obj.get("cell_list", []):
            cc = cell.get("cell_container", {})
            ue_list = cc.get("ue_list", [])
            if not ue_list: continue
            u = ue_list[0].get("ue_container", {})
            if not u: continue
            last_u = u
            if float(u.get("dl_bitrate", 0) or 0) > 0 or float(u.get("ul_bitrate", 0) or 0) > 0:
                last_traffic_u = u
            for bearer in u.get("bearer_list", []):
                bc = bearer.get("bearer_container", {})
                if bc.get("qci") == 9:
                    if last_bearer is None or (
                        float(bc.get("dl_total_bytes", 0) or 0) > float(last_bearer.get("dl_total_bytes", 0) or 0)
                    ):
                        last_bearer = bc

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
        "phy_dl_turbo_avg": 0.0, "phy_cfo_avg": 0.0, "phy_ul_ta_avg": 0.0,
        "phy_sample_count": 0,
    }

    hdr  = None; rows = []
    for line in out.splitlines():
        if line.startswith("PHY_HDR:"): hdr = line[8:].split(";")
        elif line.startswith("PHY_ROW:"): rows.append(line[8:].split(";"))

    if not hdr or not rows: return phy

    def idx_of(*keys):
        for key in keys:
            for i, h in enumerate(hdr):
                if h.strip() == key: return i
        return None

    def col_vals(*keys, src=None):
        source = src if src is not None else rows
        for key in keys:
            i = idx_of(key)
            if i is None: continue
            vals = []
            for r in source:
                if i < len(r):
                    try: vals.append(float(r[i]))
                    except (ValueError, TypeError): pass
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
        if dl_b > 0 or ul_b > 0: traffic_rows.append(r)

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


def snap_gnb_agg(n):
    script = f"""
MET="{GNB_LOG_DIR}/enb_ue{n}_metrics.csv"
if [ -f "$MET" ]; then
  echo "GAGG_HDR:$(head -1 $MET)"
  tail -30 "$MET" | grep -v '^$' | while IFS= read -r line; do echo "GAGG_ROW:$line"; done
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

    hdr  = None; rows = []
    for line in out.splitlines():
        if line.startswith("GAGG_HDR:"): hdr = line[9:].split(";")
        elif line.startswith("GAGG_ROW:"): rows.append(line[9:].split(";"))

    if not hdr or not rows: return agg

    def col_vals(key):
        idx = None
        for i, h in enumerate(hdr):
            if h.strip() == key: idx = i; break
        if idx is None: return []
        vals = []
        for r in rows:
            if idx < len(r):
                try: vals.append(float(r[idx]))
                except (ValueError, TypeError): pass
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
    agg["gnb_sys_load_avg"] = safe_avg(col_vals("system_load"))

    cpu_cols = [k for k in hdr if k.strip().startswith("cpu_")]
    all_cpu_vals = []
    for row in rows:
        row_map = dict(zip(hdr, row))
        for ck in cpu_cols:
            try:
                v = float(row_map.get(ck, 0) or 0)
                if v > 0: all_cpu_vals.append(v)
            except (ValueError, TypeError): pass
    agg["gnb_cpu_avg_pct"] = round(sum(all_cpu_vals) / len(all_cpu_vals), 2) if all_cpu_vals else 0.0
    rmem = col_vals("proc_rmem_kB") or col_vals("proc_rmem_kb") or col_vals("rmem_kb")
    agg["gnb_proc_rmem_kb_avg"] = safe_avg(rmem)
    thr = col_vals("thread_count") or col_vals("nof_threads")
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
        "gnbj_ul_pusch_rssi_avg": 0.0, "gnbj_ul_pucch_rssi_avg": 0.0,
        "gnbj_ul_pucch_ni_avg": 0.0,
        "gnbj_dl_bler_avg": 0.0, "gnbj_ul_bler_avg": 0.0,
        "gnbj_dl_bitrate_avg_mbps": 0.0, "gnbj_ul_bitrate_avg_mbps": 0.0,
        "gnbj_ul_phr_avg": 0.0,
        "gnbj_dl_total_bytes": 0.0, "gnbj_ul_total_bytes": 0.0,
        "gnbj_dl_latency_avg_ms": 0.0,
        "gnbj_sample_count": 0,
    }

    acc = {k: [] for k in gnbj}
    dec = json.JSONDecoder()
    i = 0
    while i < len(out):
        try:
            obj, idx = dec.raw_decode(out, i)
            i = idx
        except json.JSONDecodeError:
            i += 1; continue
        if not isinstance(obj, dict) or obj.get("type") != "metrics": continue

        u = {}
        for cell in obj.get("cell_list", []):
            cc = cell.get("cell_container", {})
            ue_list = cc.get("ue_list", [])
            if ue_list:
                u = ue_list[0].get("ue_container", {})
                break
        if not u: continue

        def fv(k, d=0.0):
            try: return float(u.get(k, d) or d)
            except: return d

        brate_dl = fv("dl_bitrate"); brate_ul = fv("ul_bitrate")
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

        acc["gnbj_ul_pusch_rssi_avg"].append(fv("ul_pusch_rssi"))
        acc["gnbj_ul_pucch_rssi_avg"].append(fv("ul_pucch_rssi"))
        acc["gnbj_ul_pucch_ni_avg"].append(fv("ul_pucch_ni"))

        for bearer in u.get("bearer_list", []):
            bc = bearer.get("bearer_container", {})
            if bc.get("qci") == 9:
                dl_tb = float(bc.get("dl_total_bytes", 0) or 0)
                ul_tb = float(bc.get("ul_total_bytes", 0) or 0)
                dl_lat = float(bc.get("dl_latency", 0) or 0)
                if dl_tb > 0: acc["gnbj_dl_total_bytes"].append(dl_tb)
                if ul_tb > 0: acc["gnbj_ul_total_bytes"].append(ul_tb)
                if dl_lat > 0: acc["gnbj_dl_latency_avg_ms"].append(dl_lat)
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


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\n{'#'*62}")
    print(f"  v25.10  50-UE Accumulation Experiment")
    print(f"  gnb1=pc802  uehost1=pc801  core=pc808")
    print(f"  Output: {OUT_FILE}")
    print(f"{'#'*62}\n")

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

    tmp = OUT_FILE.with_suffix(".tmp")
    with open(tmp, "w", newline="") as fh:
        w2 = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w2.writeheader()
        for r in existing_rows:
            for c in FIELDS: r.setdefault(c, "")
            w2.writerow(r)
    shutil.move(str(tmp), str(OUT_FILE))

    print(f"\nKilling leftover processes...")
    kill_all()
    restart_epc()

    ssh(GNB_HOST, f"mkdir -p {GNB_LOG_DIR}", timeout=10)
    ssh(UE_HOST,  f"mkdir -p {UE_LOG_DIR}",  timeout=10)
    setup_gtp_aliases(MAX_UE)

    print("Starting persistent iperf3 server loops on core (ports 5201-5250)...")
    ssh(CORE_HOST, "sudo pkill -9 iperf3 2>/dev/null; sleep 1; echo done", timeout=10)
    time.sleep(2)
    for n in range(1, MAX_UE + 1):
        port = 5200 + n
        loop = (f"while true; do "
                f"iperf3 -s -B {CORE_IP} -p {port} --one-off 2>/dev/null; "
                f"sleep 0.2; done")
        ssh_bg(CORE_HOST, loop)
    time.sleep(8)

    ssh(UE_HOST,
        "for n in $(seq 1 50); do sudo ip netns add ue$n 2>/dev/null || true; done; "
        "rm -f /users/saish/.ctxt /root/.ctxt 2>/dev/null; "
        "for n in $(seq 1 50); do rm -f /tmp/ue_ctx/ue$n/.ctxt 2>/dev/null; done; "
        "mkdir -p /tmp/ue_ctx; ", timeout=40)

    n_attached = 0

    def append_row(row_dict):
        desk_file = os.path.expanduser("~/Desktop/v25_10_ue_results_50.csv")
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
        row = {"ver": "v25_10", "ue_id": n, "n_attached": n_attached}

        print(f"\n{'='*62}")
        print(f"  UE {n:2d}/50  [{time.strftime('%H:%M:%S')}]  ({n_attached} UEs running)")
        print(f"{'='*62}")

        # 1. Start gNB
        print(f"  Starting gNB {n}...")
        start_gnb_ue(n)
        port = 40000 + n * 10
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

        # 2. Start srsue
        print(f"  Starting srsue {n}...")
        start_srsue(n)
        time.sleep(2)

        # 3. Wait attach
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
        row.update({"attach_ok": "OK", "attach_ms": attach_ms, "ue_ip": ue_ip})
        inject_default_route(n)
        time.sleep(PING_SETTLE_S)

        # 4. Ping
        p_avg, p_min, p_max, p_jit, p_loss = run_ping(n)
        if p_loss >= 100.0:
            healed, new_ip, new_ms = heal_upf(n)
            if healed:
                if new_ip:
                    ue_ip = new_ip; attach_ms = new_ms
                    row.update({"ue_ip": ue_ip, "attach_ms": attach_ms})
                p_avg, p_min, p_max, p_jit, p_loss = run_ping(n)

        row.update({"ping_avg_ms": p_avg, "ping_min_ms": p_min,
                     "ping_max_ms": p_max, "ping_jitter_ms": p_jit,
                     "ping_loss_pct": p_loss})

        # 5. DL iperf
        for r in DL_RATES:
            if p_loss >= 100.0: mbps, loss = 0.0, 100.0
            else:
                mbps, loss = run_iperf(n, r, "dl")
                time.sleep(1)
            row[f"dl_{r}m_mbps"] = mbps
            row[f"dl_{r}m_loss_pct"] = loss

        # 6. UL iperf
        ran_bg = {}; agg_bg = {}; gnbj_bg = {}; phy_bg = {}
        ul_snap_done = False
        for idx_r, r in enumerate(UL_RATES):
            if p_loss >= 100.0: mbps, loss = 0.0, 100.0
            else:
                mbps, loss = run_iperf(n, r, "ul")
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

        row.update(ran_bg if ran_bg else snap_gnb_ran(n))
        row.update(snap_gnb_metrics())
        row.update(agg_bg if agg_bg else snap_gnb_agg(n))
        row.update(gnbj_bg if gnbj_bg else snap_gnbj(n))
        row.update(phy_bg if phy_bg else snap_ue_phy(n))

        append_row(row)
        print(f"  ✓ UE{n} saved (synced to Desktop).")

    print(f"\n{'#'*62}\n  v25.10 experiment complete!\n{'#'*62}")

if __name__ == "__main__":
    main()
