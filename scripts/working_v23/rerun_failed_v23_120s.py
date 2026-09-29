#!/usr/bin/env python3
"""
rerun_failed_v23_120s.py — Redo experiment for failed UEs in v23.11 with 120s timeout.
===================================================================================
1. Reads existing results/ver_eval/v23_11/ue_results_50.csv
2. Identifies all good UEs and all failed/missing UEs.
3. Kills leftovers, restarts EPC fresh.
4. Iterates 1..50 in sequence:
   - For PREVIOUSLY GOOD UEs: Fast-connects them (launches gNB + UE + route) to build background load,
     and re-uses the existing measurements.
   - For FAILED UEs (e.g. 30, 36, 38, 40..50): Runs full attach with ATTACH_TIMEOUT = 120s,
     and performs the complete 170-column measurement suite (ping, 36 iperf rates, RAN/power telemetry).
5. Updates and preserves all rows in:
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

ATTACH_TIMEOUT = 120    # 120 seconds extended timeout
PING_COUNT     = 20
PING_SETTLE_S  = 5
DL_RATES = list(range(1, 11)) + list(range(15, 51, 5))   # 18 rates
UL_RATES = list(range(1, 11)) + list(range(15, 51, 5))   # 18 rates
IPERF_DUR      = 10
MAX_UE         = 50
CORE_IP        = "10.45.0.1"

OUT_DIR  = pathlib.Path("results/ver_eval/v23_11")
OUT_FILE = OUT_DIR / "ue_results_50.csv"
DESK_FILE= pathlib.Path(os.path.expanduser("~/Desktop/v23_11_ue_results_50.csv"))

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


def kill_all():
    print("  Killing srsenb (pc802) & srsue (pc801)...")
    ssh(GNB_HOST, "sudo killall srsenb 2>/dev/null; sleep 1; "
                  "sudo killall -9 srsenb 2>/dev/null; sleep 1; echo done")
    ssh(UE_HOST,  "sudo killall srsue  2>/dev/null; sleep 4; "
                  "sudo killall -9 srsue 2>/dev/null; sleep 2; echo done")


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
    cfg     = f"{ENB_CONF_DIR}/enb_ue{n}.conf"
    log     = f"{GNB_LOG_DIR}/enb_ue{n}.log"
    met_csv = f"{GNB_LOG_DIR}/enb_ue{n}_metrics.csv"
    rpt_json= f"{GNB_LOG_DIR}/enb_ue{n}_report.json"
    ssh(GNB_HOST,
        f"mkdir -p {GNB_LOG_DIR} && rm -f {log} {met_csv} {rpt_json}",
        timeout=10)
    ssh_bg(GNB_HOST,
           f"sudo {SRSENB_BIN} {cfg} "
           f"--expert.metrics_csv_enable=1 "
           f"--expert.metrics_csv_filename={met_csv} "
           f"--expert.metrics_period_secs=1 "
           f"--expert.report_json_enable=1 "
           f"--expert.report_json_filename={rpt_json} "
           f"> {log} 2>&1")


def wait_gnb_port(n, timeout=25):
    port = 40000 + n * 10
    t0 = time.time()
    time.sleep(2)
    while time.time() - t0 < timeout:
        out, _ = ssh(GNB_HOST,
                     f"ss -tulpn | grep ':{port} ' || ss -tulpn | grep ':{port}$'",
                     timeout=5)
        if out.strip():
            return True
        time.sleep(1)
    return False


def inject_default_route(n):
    out, _ = ssh(UE_HOST,
        f"TUN=$(sudo ip netns exec ue{n} ip -br a 2>/dev/null | grep -o 'tun_ue{n}\\|tun_srsue{n}' | head -1); "
        f"if [ -n \"$TUN\" ]; then "
        f"  sudo ip netns exec ue{n} ip route del default 2>/dev/null || true; "
        f"  sudo ip netns exec ue{n} ip route add 10.45.0.1 dev $TUN 2>/dev/null || true; "
        f"  sudo ip netns exec ue{n} ip route add default via 10.45.0.1 dev $TUN 2>/dev/null || true; "
        f"  sudo ip netns exec ue{n} ip route show; "
        f"else echo 'NO_TUN'; fi",
        timeout=12)
    return "10.45.0.1" in out and "default" in out


def setup_gtp_aliases(max_n=50):
    alias_script = f"""
for n in $(seq 1 {max_n}); do
    sudo ip addr add {GNB_GTP_BASE}.$n/24 dev {GNB_LAN_IF} 2>/dev/null || true
done
ip -br a show dev {GNB_LAN_IF} | awk '{{print $1, $3}}'
"""
    ssh(GNB_HOST, alias_script, timeout=30)
    patch_script = f"""
for n in $(seq 1 {max_n}); do
    sudo sed -i -E "s/gtp_bind_addr = .*/gtp_bind_addr = {GNB_GTP_BASE}.$n/" \\
      {ENB_CONF_DIR}/enb_ue${{n}}.conf 2>/dev/null;
    sudo sed -i -E "s/s1c_bind_addr = .*/s1c_bind_addr = {GNB_GTP_BASE}.$n/" \\
      {ENB_CONF_DIR}/enb_ue${{n}}.conf 2>/dev/null;
done
echo "gtp_patched:$(grep -c 'gtp_bind_addr = 10.10.2' {ENB_CONF_DIR}/enb_ue*.conf 2>/dev/null)"
"""
    ssh(GNB_HOST, patch_script, timeout=30)
    route_script = f"sudo ip route replace {GNB_GTP_BASE}.0/24 via 10.10.1.2 dev eth1 2>/dev/null || true; ip r | grep {GNB_GTP_BASE}"
    ssh(CORE_HOST, route_script, timeout=15)


def precreate_netns(max_n=50):
    cmd = (
        f"for n in $(seq 1 {max_n}); do sudo ip netns add ue$n 2>/dev/null || true; done; "
        f"echo netns_count=$(ip netns list | wc -l)"
    )
    ssh(UE_HOST, cmd, timeout=30)


def start_srsue(n):
    cfg     = f"{UE_CONF_DIR}/ue{n}.conf"
    log     = f"{UE_LOG_DIR}/ue{n}.log"
    ue_csv  = f"{UE_LOG_DIR}/ue{n}_metrics.csv"
    ctx_dir = f"/tmp/ue_ctx/ue{n}"
    ssh(UE_HOST,
        f"mkdir -p {UE_LOG_DIR} {ctx_dir} && "
        f"rm -f {log} {ue_csv} {ctx_dir}/.ctxt /tmp/ue_ctx/ue{n}/.ctxt /root/.ctxt",
        timeout=10)
    ssh_bg(UE_HOST,
           f"sudo HOME={ctx_dir} {SRSUE_BIN} {cfg} "
           f"--general.metrics_csv_enable=1 "
           f"--general.metrics_csv_filename={ue_csv} "
           f"--general.metrics_period_secs=1 "
           f"> {log} 2>&1")


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
    ssh(UE_HOST, f"sudo kill -9 $(pgrep -f 'ue{ue_n}.conf') 2>/dev/null || true; sleep 1", timeout=10)
    ssh(GNB_HOST, f"sudo kill -9 $(pgrep -f 'enb_ue{ue_n}.conf') 2>/dev/null || true; sleep 1", timeout=10)
    heal_script = """
sudo systemctl restart open5gs-upfd  && sleep 2
sudo systemctl restart open5gs-sgwud && sleep 2
sudo ip route replace 10.10.2.0/24 via 10.10.1.2 dev eth1 2>/dev/null || true
echo "upf_sgwu_restarted"
"""
    out, _ = ssh(CORE_HOST, heal_script, timeout=30)
    print(f"  [HEAL] Step 2/4: UPF+SGW-U restart: {out}")
    print(f"  [HEAL] Step 3/4: re-starting srsenb{ue_n}...")
    start_gnb_ue(ue_n)
    time.sleep(2)
    if not wait_gnb_port(ue_n, timeout=20):
        print(f"  [HEAL] srsenb{ue_n} port not bound — heal failed")
        return False, "", 0
    print(f"  [HEAL] Step 4/4: re-attaching srsue{ue_n} (timeout={ATTACH_TIMEOUT}s)...")
    start_srsue(ue_n)
    new_ip, new_ms = wait_attach(ue_n)
    if not new_ip:
        print(f"  [HEAL] Re-attach timed out — heal failed")
        return False, "", 0
    print(f"  [HEAL] Re-attached: IP={new_ip}  {new_ms}ms")
    inject_default_route(ue_n)
    time.sleep(PING_SETTLE_S)
    p_avg, _, _, _, p_loss = run_ping(ue_n, count=5)
    print(f"  [HEAL] Post-heal ping: avg={p_avg}ms loss={p_loss}%")
    if p_loss < 100.0:
        return True, new_ip, new_ms
    return False, new_ip, new_ms


def ensure_iperf_server(n):
    port = 5200 + n
    out, _ = ssh(CORE_HOST, f"pgrep -f 'iperf3.*-p {port}' || true", timeout=5)
    if not out.strip():
        loop = (
            f"while true; do "
            f"  /usr/bin/iperf3 -s -p {port} --one-off -B 0.0.0.0 >/dev/null 2>&1; "
            f"  sleep 0.1; "
            f"done"
        )
        ssh_bg(CORE_HOST, loop)
        time.sleep(1)


def run_iperf(n, rate_mbps, direction="dl"):
    port = 5200 + n
    ensure_iperf_server(n)
    if direction == "dl":
        cmd = (f"sudo ip netns exec ue{n} timeout {IPERF_DUR + 8} "
               f"iperf3 -c {CORE_IP} -u -b {rate_mbps}M -t {IPERF_DUR} -p {port} -R -J")
    else:
        cmd = (f"sudo ip netns exec ue{n} timeout {IPERF_DUR + 8} "
               f"iperf3 -c {CORE_IP} -u -b {rate_mbps}M -t {IPERF_DUR} -p {port} -J")
    out, _ = ssh(UE_HOST, cmd, timeout=IPERF_DUR + 15)
    try:
        j = json.loads(out)
        sent = j.get("end", {}).get("sum", {})
        rcvd = j.get("end", {}).get("sum_received", {}) or sent
        bps  = rcvd.get("bits_per_second", 0.0)
        mbps = round(bps / 1e6, 4)
        loss = round(float(rcvd.get("lost_percent", 0.0)), 2)
        return mbps, loss
    except Exception:
        pass
    try:
        bm = re.search(r'([\d.]+)\s+Mbits/sec\s+([\d.]+)%', out)
        if bm:
            return round(float(bm.group(1)), 4), round(float(bm.group(2)), 2)
        bm2 = re.search(r'([\d.]+)\s+Mbits/sec', out)
        if bm2:
            return round(float(bm2.group(1)), 4), 0.0
    except Exception:
        pass
    return 0.0, 100.0


def snap_gnb_metrics():
    script = """
TS_OUT=$(mktemp /tmp/ts_XXXXXX.txt)
sudo timeout 4 turbostat --quiet --show Busy%,Bzy_MHz,IPC,IRQ,SMI,CoreTmp,PkgTmp,Pkg%pc2,Pkg%pc3,PkgWatt,RAMWatt,Pkg%_w,RAM%_w,CPU%c1,CPU%c3,CPU%c6,CPU%c7,Avg_MHz \
  --interval 2 --num_iterations 1 > "$TS_OUT" 2>&1
cat "$TS_OUT"; rm -f "$TS_OUT"
echo "---RAPL---"
sudo cat /sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj 2>/dev/null || echo 0
sudo cat /sys/class/powercap/intel-rapl/intel-rapl:1/energy_uj 2>/dev/null || echo 0
sleep 1
sudo cat /sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj 2>/dev/null || echo 0
sudo cat /sys/class/powercap/intel-rapl/intel-rapl:1/energy_uj 2>/dev/null || echo 0
echo "---SYS---"
cat /proc/loadavg
awk '{sum+=$1; n++} END {if(n>0) print sum/n/1000; else print 0}' /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq 2>/dev/null || echo 0
awk 'BEGIN{max=0} {if($1>max) max=$1} END {print max/1000}' /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq 2>/dev/null || echo 0
ps -C srsenb -o %cpu= 2>/dev/null | awk '{s+=$1} END {print s+0}'
"""
    out, _ = ssh(GNB_HOST, script, timeout=22)
    res = {
        "ts_avg_mhz": 0.0, "ts_busy_pct": 0.0, "ts_bzy_mhz": 0.0, "ts_ipc": 0.0,
        "ts_irq": 0, "ts_smi": 0, "ts_c1_pct": 0.0, "ts_c3_pct": 0.0, "ts_c6_pct": 0.0,
        "ts_c7_pct": 0.0, "ts_core_tmp": 0, "ts_pkg_tmp": 0, "ts_pkg_pc2_pct": 0.0,
        "ts_pkg_pc3_pct": 0.0, "ts_pkg_watt": 0.0, "ts_ram_watt": 0.0, "ts_pkg_pct": 0.0,
        "ts_ram_pct": 0.0, "cpu_freq_avg_mhz": 0.0, "cpu_freq_max_mhz": 0.0,
        "load1": 0.0, "load5": 0.0, "load15": 0.0, "rapl_pkg0_w": 0.0, "rapl_pkg1_w": 0.0,
        "rapl_total_w": 0.0, "srsenb_cpu_pct": 0.0,
        "cpu_user_pct": 0.0, "cpu_sys_pct": 0.0, "temp_c": 0.0
    }
    if not out or out == "TIMEOUT":
        return res
    parts = out.split("---RAPL---")
    ts_part = parts[0]
    rest    = parts[1] if len(parts) > 1 else ""
    rapl_part, sys_part = rest.split("---SYS---") if "---SYS---" in rest else (rest, "")
    lines = [l.strip() for l in ts_part.strip().splitlines() if l.strip()]
    if len(lines) >= 2:
        hdr = lines[0].split()
        val = lines[-1].split()
        if len(hdr) == len(val):
            d = dict(zip(hdr, val))
            def f(k):
                try: return float(d.get(k, 0.0))
                except: return 0.0
            res["ts_avg_mhz"]     = f("Avg_MHz")
            res["ts_busy_pct"]    = f("Busy%")
            res["ts_bzy_mhz"]     = f("Bzy_MHz")
            res["ts_ipc"]         = f("IPC")
            res["ts_irq"]         = int(f("IRQ"))
            res["ts_smi"]         = int(f("SMI"))
            res["ts_c1_pct"]      = f("CPU%c1")
            res["ts_c3_pct"]      = f("CPU%c3")
            res["ts_c6_pct"]      = f("CPU%c6")
            res["ts_c7_pct"]      = f("CPU%c7")
            res["ts_core_tmp"]    = f("CoreTmp")
            res["ts_pkg_tmp"]     = f("PkgTmp")
            res["ts_pkg_pc2_pct"] = f("Pkg%pc2")
            res["ts_pkg_pc3_pct"] = f("Pkg%pc3")
            res["ts_pkg_watt"]    = f("PkgWatt")
            res["ts_ram_watt"]    = f("RAMWatt")
            res["ts_pkg_pct"]     = f("Pkg%_w")
            res["ts_ram_pct"]     = f("RAM%_w")
            res["cpu_user_pct"]   = f("Busy%")
            res["temp_c"]         = f("CoreTmp")
    r_lines = [l.strip() for l in rapl_part.strip().splitlines() if l.strip()]
    if len(r_lines) >= 4:
        try:
            p0_0, p1_0 = float(r_lines[0]), float(r_lines[1])
            p0_1, p1_1 = float(r_lines[2]), float(r_lines[3])
            w0 = max(0.0, (p0_1 - p0_0) / 1e6)
            w1 = max(0.0, (p1_1 - p1_0) / 1e6)
            res["rapl_pkg0_w"]  = round(w0, 3)
            res["rapl_pkg1_w"]  = round(w1, 3)
            res["rapl_total_w"] = round(w0 + w1, 3)
        except Exception:
            pass
    s_lines = [l.strip() for l in sys_part.strip().splitlines() if l.strip()]
    if len(s_lines) >= 1:
        lp = s_lines[0].split()
        if len(lp) >= 3:
            try:
                res["load1"]  = float(lp[0])
                res["load5"]  = float(lp[1])
                res["load15"] = float(lp[2])
            except: pass
    if len(s_lines) >= 2:
        try: res["cpu_freq_avg_mhz"] = round(float(s_lines[1]), 1)
        except: pass
    if len(s_lines) >= 3:
        try: res["cpu_freq_max_mhz"] = round(float(s_lines[2]), 1)
        except: pass
    if len(s_lines) >= 4:
        try: res["srsenb_cpu_pct"] = round(float(s_lines[3]), 1)
        except: pass
    return res


def snap_gnb_ran(n):
    script = f"""
MET="{GNB_LOG_DIR}/enb_ue{n}_metrics.csv"
RPT="{GNB_LOG_DIR}/enb_ue{n}_report.json"
echo "===MET==="
if [ -f "$MET" ]; then
    tail -n 5 "$MET"
fi
echo "===RPT==="
if [ -f "$RPT" ]; then
    tail -n 40 "$RPT"
fi
"""
    out, _ = ssh(GNB_HOST, script, timeout=15)
    r = {
        "dl_mcs": 0.0, "ul_mcs": 0.0, "dl_brate_mbps": 0.0, "ul_brate_mbps": 0.0,
        "dl_prb": 0.0, "ul_prb": 0.0, "dl_tbs": 0.0, "ul_tbs": 0.0,
        "dl_bler": 0.0, "ul_bler": 0.0, "cqi": 15.0, "ri": 0,
        "pusch_sinr": 0.0, "phr": 0.0, "nof_ue": 1, "system_load": 0.0,
        "proc_rmem_kb": 0, "thread_count": 0, "gnb_cpu_avg_pct": 0.0
    }
    if not out or out == "TIMEOUT":
        return r
    parts = out.split("===RPT===")
    met_part = parts[0].replace("===MET===", "").strip()
    rpt_part = parts[1].strip() if len(parts) > 1 else ""
    met_lines = [l.strip() for l in met_part.splitlines() if l.strip()]
    if len(met_lines) >= 2:
        hdr = [h.strip() for h in met_lines[0].split(";")]
        for line in reversed(met_lines[1:]):
            vals = [v.strip() for v in line.split(";")]
            if len(vals) == len(hdr):
                row_map = dict(zip(hdr, vals))
                def g(k):
                    try: return float(row_map.get(k, 0.0))
                    except: return 0.0
                dl_b = g("dl_brate")
                ul_b = g("ul_brate")
                r["dl_brate_mbps"]   = round(dl_b / 1e6, 4) if dl_b > 1000 else round(dl_b, 4)
                r["ul_brate_mbps"]   = round(ul_b / 1e6, 4) if ul_b > 1000 else round(ul_b, 4)
                r["nof_ue"]          = int(g("nof_ue")) or 1
                r["system_load"]     = g("sys_load") or g("system_load")
                r["proc_rmem_kb"]    = int(g("proc_rmem_kB") or g("proc_rmem") or 0)
                r["thread_count"]    = int(g("nof_threads") or g("thread_count") or 0)
                r["gnb_cpu_avg_pct"] = g("cpu_pct") or g("cpu_avg")
                break
    if rpt_part:
        last_obj = None
        buf = ""
        for line in rpt_part.splitlines():
            buf += line + "\n"
            try:
                last_obj = json.loads(buf)
                buf = ""
            except json.JSONDecodeError:
                pass
        if not last_obj:
            for l in reversed(rpt_part.splitlines()):
                l = l.strip()
                if l.startswith("{") and l.endswith("}"):
                    try: last_obj = json.loads(l); break
                    except: pass
        if last_obj and isinstance(last_obj, dict):
            ue_list = None
            cell_list = last_obj.get("cell_list", [])
            if cell_list:
                cc = cell_list[0].get("cell_container", cell_list[0])
                ue_list = cc.get("ue_list", [])
            if not ue_list:
                ue_list = last_obj.get("ue_list", [])
            if ue_list:
                ue0 = ue_list[0].get("ue_container", ue_list[0])
                def uf(k, d=0.0):
                    try: return float(ue0.get(k, d))
                    except: return d
                dl_m = uf("dl_mcs")
                ul_m = uf("ul_mcs")
                if dl_m > 0: r["dl_mcs"] = round(dl_m, 6)
                if ul_m > 0: r["ul_mcs"] = round(ul_m, 6)
                cqi_v = uf("cqi", 15.0)
                if cqi_v > 0: r["cqi"] = round(cqi_v, 1)
                r["ri"] = int(uf("ri", 0))
                sinr_v = uf("pusch_snr") or uf("pusch_sinr") or uf("sinr")
                if sinr_v: r["pusch_sinr"] = round(sinr_v, 6)
                phr_v = uf("phr")
                if phr_v: r["phr"] = round(phr_v, 6)
                dl_bler_v = uf("dl_bler")
                ul_bler_v = uf("ul_bler")
                if dl_bler_v: r["dl_bler"] = round(dl_bler_v, 6)
                if ul_bler_v: r["ul_bler"] = round(ul_bler_v, 6)
    return r


def snap_ue_phy(n):
    script = f"""
UE_CSV="{UE_LOG_DIR}/ue{n}_metrics.csv"
if [ -f "$UE_CSV" ]; then
    tail -n 35 "$UE_CSV"
fi
"""
    out, _ = ssh(UE_HOST, script, timeout=15)
    res = {
        "dl_mcs_avg": 0.0, "dl_mcs_max": 0.0,
        "ul_mcs_avg": 0.0, "ul_mcs_max": 0.0,
        "dl_snr_avg": 0.0, "dl_snr_max": 0.0,
        "rsrp_avg": 0.0, "pathloss_avg": 0.0,
        "dl_brate_peak_mbps": 0.0, "dl_brate_avg_mbps": 0.0,
        "ul_brate_peak_mbps": 0.0, "ul_brate_avg_mbps": 0.0,
        "dl_bler_avg": 0.0, "ul_bler_avg": 0.0,
        "dl_turbo_avg": 0.0, "cfo_avg": 0.0, "ul_ta_avg": 0.0,
        "sample_count": 0
    }
    if not out or out == "TIMEOUT":
        return res
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    if len(lines) < 2:
        return res
    hdr_line = lines[0]
    sep = ";" if ";" in hdr_line else ","
    hdr  = [h.strip().lower() for h in hdr_line.split(sep)]
    rows = []
    for l in lines[1:]:
        v = [x.strip() for x in l.split(sep)]
        if len(v) == len(hdr):
            rows.append(v)
    if not rows:
        return res
    def idx_of(*keys):
        for k in keys:
            if k in hdr:
                return hdr.index(k)
        return None
    def col_vals(*keys, src=None):
        target = src if src is not None else rows
        for k in keys:
            if k in hdr:
                idx = hdr.index(k)
                vals = []
                for r in target:
                    try:
                        v_str = r[idx].strip()
                        if v_str != "":
                            vals.append(float(v_str))
                    except (ValueError, IndexError):
                        pass
                if vals:
                    return vals
        return []
    def safe_avg(vals): return round(sum(vals) / len(vals), 4) if vals else 0.0
    def safe_max(vals): return round(max(vals), 4) if vals else 0.0
    res["sample_count"] = len(rows)
    res["dl_mcs_avg"] = safe_avg(col_vals("dl_mcs", "dl-mcs", "rx_mcs"))
    res["dl_mcs_max"] = safe_max(col_vals("dl_mcs", "dl-mcs", "rx_mcs"))
    res["ul_mcs_avg"] = safe_avg(col_vals("ul_mcs", "ul-mcs", "tx_mcs"))
    res["ul_mcs_max"] = safe_max(col_vals("ul_mcs", "ul-mcs", "tx_mcs"))
    res["dl_snr_avg"] = safe_avg(col_vals("dl_snr", "snr", "rx_snr", "sinr"))
    res["dl_snr_max"] = safe_max(col_vals("dl_snr", "snr", "rx_snr", "sinr"))
    res["rsrp_avg"]   = safe_avg(col_vals("rsrp", "dl_rsrp", "rx_rsrp"))
    res["pathloss_avg"] = safe_avg(col_vals("pl", "pathloss", "dl_pathloss", "dl_pl"))
    def _to_f(val):
        try:
            return float(val.strip()) if val.strip() != "" else 0.0
        except (ValueError, AttributeError):
            return 0.0
    dl_b_idx = idx_of("dl_brate", "dl_rate", "rx_rate", "dl_bitrate")
    if dl_b_idx is not None:
        raw_b = [_to_f(r[dl_b_idx]) for r in rows if r[dl_b_idx].strip() != ""]
        mb = [x / 1e6 if x > 1000 else x for x in raw_b if x > 0]
        if mb:
            res["dl_brate_avg_mbps"]  = safe_avg(mb)
            res["dl_brate_peak_mbps"] = safe_max(mb)
    ul_b_idx = idx_of("ul_brate", "ul_rate", "tx_rate", "ul_bitrate")
    if ul_b_idx is not None:
        raw_b = [_to_f(r[ul_b_idx]) for r in rows if r[ul_b_idx].strip() != ""]
        mb = [x / 1e6 if x > 1000 else x for x in raw_b if x > 0]
        if mb:
            res["ul_brate_avg_mbps"]  = safe_avg(mb)
            res["ul_brate_peak_mbps"] = safe_max(mb)
    res["dl_bler_avg"]  = safe_avg(col_vals("dl_bler", "rx_bler", "bler_dl"))
    res["ul_bler_avg"]  = safe_avg(col_vals("ul_bler", "tx_bler", "bler_ul"))
    res["dl_turbo_avg"] = safe_avg(col_vals("turbo_iters", "dl_turbo", "turbo"))
    res["cfo_avg"]      = safe_avg(col_vals("cfo", "cfo_hz", "dl_cfo"))
    res["ul_ta_avg"]    = safe_avg(col_vals("ta", "ul_ta", "time_adv"))
    return res


def snap_gnb_agg(n):
    script = f"""
MET="{GNB_LOG_DIR}/enb_ue{n}_metrics.csv"
if [ -f "$MET" ]; then
    tail -n 30 "$MET"
fi
"""
    out, _ = ssh(GNB_HOST, script, timeout=12)
    res = {
        "dl_brate_peak_mbps": 0.0, "dl_brate_avg_mbps": 0.0,
        "ul_brate_peak_mbps": 0.0, "ul_brate_avg_mbps": 0.0,
        "sys_load_avg": 0.0, "cpu_avg_pct": 0.0,
        "proc_rmem_kb_avg": 0.0, "thread_count": 0, "sample_count": 0
    }
    if not out or out == "TIMEOUT":
        return res
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    if len(lines) < 2:
        return res
    hdr_line = lines[0]
    sep = ";" if ";" in hdr_line else ","
    hdr = [h.strip().lower() for h in hdr_line.split(sep)]
    rows = []
    for l in lines[1:]:
        v = [x.strip() for x in l.split(sep)]
        if len(v) == len(hdr):
            rows.append(v)
    if not rows:
        return res
    res["sample_count"] = len(rows)
    def col_vals(key):
        if key in hdr:
            idx = hdr.index(key)
            vals = []
            for r in rows:
                try:
                    v_str = r[idx].strip()
                    if v_str != "":
                        vals.append(float(v_str))
                except (ValueError, IndexError):
                    pass
            return vals
        return []
    def safe_avg(vals): return round(sum(vals) / len(vals), 4) if vals else 0.0
    def safe_max(vals): return round(max(vals), 4) if vals else 0.0
    raw_dl = col_vals("dl_brate")
    if raw_dl:
        mb = [x / 1e6 if x > 1000 else x for x in raw_dl if x > 0]
        res["dl_brate_avg_mbps"]  = safe_avg(mb)
        res["dl_brate_peak_mbps"] = safe_max(mb)
    raw_ul = col_vals("ul_brate")
    if raw_ul:
        mb = [x / 1e6 if x > 1000 else x for x in raw_ul if x > 0]
        res["ul_brate_avg_mbps"]  = safe_avg(mb)
        res["ul_brate_peak_mbps"] = safe_max(mb)
    res["sys_load_avg"]     = safe_avg(col_vals("sys_load") or col_vals("system_load"))
    res["cpu_avg_pct"]      = safe_avg(col_vals("cpu_pct") or col_vals("cpu_avg") or col_vals("cpu%"))
    res["proc_rmem_kb_avg"] = safe_avg(col_vals("proc_rmem_kb") or col_vals("proc_rmem"))
    tc = col_vals("nof_threads") or col_vals("thread_count")
    res["thread_count"]     = int(tc[-1]) if tc else 0
    return res


def snap_gnbj(n):
    script = f"""
RPT="{GNB_LOG_DIR}/enb_ue{n}_report.json"
if [ -f "$RPT" ]; then
    tail -n 400 "$RPT"
fi
"""
    out, _ = ssh(GNB_HOST, script, timeout=15)
    res = {
        "dl_cqi_avg": 0.0, "dl_mcs_avg": 0.0, "dl_mcs_max": 0.0,
        "ul_mcs_avg": 0.0, "ul_mcs_max": 0.0, "ul_snr_avg": 0.0,
        "ul_pusch_rssi_avg": 0.0, "ul_pucch_rssi_avg": 0.0, "ul_pucch_ni_avg": 0.0,
        "dl_bler_avg": 0.0, "ul_bler_avg": 0.0,
        "dl_bitrate_avg_mbps": 0.0, "ul_bitrate_avg_mbps": 0.0,
        "ul_phr_avg": 0.0, "dl_total_bytes": 0.0, "ul_total_bytes": 0.0,
        "dl_latency_avg_ms": 0.0, "sample_count": 0
    }
    if not out or out == "TIMEOUT":
        return res
    objs = []
    buf = ""
    for line in out.splitlines():
        line_s = line.strip()
        if not line_s: continue
        buf += line_s
        try:
            o = json.loads(buf)
            objs.append(o)
            buf = ""
        except json.JSONDecodeError:
            pass
    if not objs:
        for l in out.splitlines():
            l = l.strip()
            if l.startswith("{") and l.endswith("}"):
                try: objs.append(json.loads(l))
                except: pass
    if not objs:
        return res
    acc = {
        "dl_cqi": [], "dl_mcs": [], "ul_mcs": [], "ul_snr": [],
        "pusch_rssi": [], "pucch_rssi": [], "pucch_ni": [],
        "dl_bler": [], "ul_bler": [], "dl_brate": [], "ul_brate": [],
        "phr": [], "dl_bytes": [], "ul_bytes": [], "dl_lat": []
    }
    for obj in objs:
        if not isinstance(obj, dict): continue
        ue_list = None
        cl = obj.get("cell_list", [])
        if cl:
            cc = cl[0].get("cell_container", cl[0])
            ue_list = cc.get("ue_list", [])
        if not ue_list:
            ue_list = obj.get("ue_list", [])
        if not ue_list: continue
        ue0 = ue_list[0].get("ue_container", ue_list[0])
        def fv(k, d=0.0):
            try: return float(ue0.get(k, d))
            except: return d
        cqi = fv("cqi");   dl_m = fv("dl_mcs"); ul_m = fv("ul_mcs")
        snr = fv("pusch_snr") or fv("pusch_sinr") or fv("sinr")
        p_rssi = fv("pusch_rssi"); puc_rssi = fv("pucch_rssi"); puc_ni = fv("pucch_ni")
        dl_bl  = fv("dl_bler");    ul_bl    = fv("ul_bler")
        dl_br  = fv("dl_brate") or fv("dl_bitrate")
        ul_br  = fv("ul_brate") or fv("ul_bitrate")
        phr    = fv("phr")
        dl_by  = fv("dl_total_bytes") or fv("dl_bytes") or fv("dl_tx_bytes")
        ul_by  = fv("ul_total_bytes") or fv("ul_bytes") or fv("ul_rx_bytes")
        lat    = fv("dl_latency") or fv("latency") or fv("rtt")
        if cqi > 0:    acc["dl_cqi"].append(cqi)
        if dl_m > 0:   acc["dl_mcs"].append(dl_m)
        if ul_m > 0:   acc["ul_mcs"].append(ul_m)
        if snr != 0.0: acc["ul_snr"].append(snr)
        if p_rssi != 0.0:   acc["pusch_rssi"].append(p_rssi)
        if puc_rssi != 0.0: acc["pucch_rssi"].append(puc_rssi)
        if puc_ni != 0.0:   acc["pucch_ni"].append(puc_ni)
        if dl_bl >= 0: acc["dl_bler"].append(dl_bl)
        if ul_bl >= 0: acc["ul_bler"].append(ul_bl)
        if dl_br > 0:  acc["dl_brate"].append(dl_br)
        if ul_br > 0:  acc["ul_brate"].append(ul_br)
        if phr != 0.0: acc["phr"].append(phr)
        if dl_by > 0:  acc["dl_bytes"].append(dl_by)
        if ul_by > 0:  acc["ul_bytes"].append(ul_by)
        if lat > 0:    acc["dl_lat"].append(lat)
    res["sample_count"] = len(objs)
    def sa(k): return round(sum(acc[k]) / len(acc[k]), 4) if acc[k] else 0.0
    def sm(k): return round(max(acc[k]), 4) if acc[k] else 0.0
    res["dl_cqi_avg"]           = sa("dl_cqi") or 15.0
    res["dl_mcs_avg"]           = sa("dl_mcs")
    res["dl_mcs_max"]           = sm("dl_mcs")
    res["ul_mcs_avg"]           = sa("ul_mcs")
    res["ul_mcs_max"]           = sm("ul_mcs")
    res["ul_snr_avg"]           = sa("ul_snr")
    res["ul_pusch_rssi_avg"]    = sa("pusch_rssi")
    res["ul_pucch_rssi_avg"]    = sa("pucch_rssi")
    res["ul_pucch_ni_avg"]      = sa("pucch_ni")
    res["dl_bler_avg"]          = sa("dl_bler")
    res["ul_bler_avg"]          = sa("ul_bler")
    raw_dl_b = acc["dl_brate"]
    if raw_dl_b:
        mb = [x / 1e6 if x > 1000 else x for x in raw_dl_b if x > 0]
        res["dl_bitrate_avg_mbps"] = round(sum(mb) / len(mb), 4) if mb else 0.0
    raw_ul_b = acc["ul_brate"]
    if raw_ul_b:
        mb = [x / 1e6 if x > 1000 else x for x in raw_ul_b if x > 0]
        res["ul_bitrate_avg_mbps"] = round(sum(mb) / len(mb), 4) if mb else 0.0
    res["ul_phr_avg"]           = sa("phr")
    res["dl_total_bytes"]       = max(acc["dl_bytes"]) if acc["dl_bytes"] else 0.0
    res["ul_total_bytes"]       = max(acc["ul_bytes"]) if acc["ul_bytes"] else 0.0
    res["dl_latency_avg_ms"]    = sa("dl_lat")
    return res


# ── CSV schema — 170 columns ──────────────────────────────────────────────────
FIELDS = (
    ["ver", "ue_id", "n_attached", "attach_ms", "attach_ok", "ue_ip",
     "ping_avg_ms", "ping_min_ms", "ping_max_ms", "ping_jitter_ms", "ping_loss_pct"]
    + [f"dl_{r}m_mbps"     for r in DL_RATES]
    + [f"dl_{r}m_loss_pct" for r in DL_RATES]
    + [f"ul_{r}m_mbps"     for r in UL_RATES]
    + [f"ul_{r}m_loss_pct" for r in UL_RATES]
    + ["ran_dl_mcs", "ran_ul_mcs", "ran_dl_brate_mbps", "ran_ul_brate_mbps",
       "ran_dl_prb", "ran_ul_prb", "ran_dl_tbs", "ran_ul_tbs",
       "ran_dl_bler", "ran_ul_bler", "ran_cqi", "ran_ri",
       "ran_pusch_sinr", "ran_phr", "ran_nof_ue", "ran_system_load",
       "ran_proc_rmem_kb", "ran_thread_count", "ran_gnb_cpu_avg_pct"]
    + ["gnb_ts_avg_mhz", "gnb_ts_busy_pct", "gnb_ts_bzy_mhz", "gnb_ts_ipc",
       "gnb_ts_irq", "gnb_ts_smi", "gnb_ts_c1_pct", "gnb_ts_c3_pct",
       "gnb_ts_c6_pct", "gnb_ts_c7_pct", "gnb_ts_core_tmp", "gnb_ts_pkg_tmp",
       "gnb_ts_pkg_pc2_pct", "gnb_ts_pkg_pc3_pct", "gnb_ts_pkg_watt",
       "gnb_ts_ram_watt", "gnb_ts_pkg_pct", "gnb_ts_ram_pct",
       "gnb_cpu_freq_avg_mhz", "gnb_cpu_freq_max_mhz",
       "gnb_load1", "gnb_load5", "gnb_load15",
       "gnb_rapl_pkg0_w", "gnb_cpu_user_pct", "gnb_cpu_sys_pct",
       "gnb_temp_c", "gnb_rapl_pkg1_w", "gnb_rapl_total_w", "gnb_srsenb_cpu_pct"]
    + ["phy_dl_mcs_avg", "phy_dl_mcs_max", "phy_ul_mcs_avg", "phy_ul_mcs_max",
       "phy_dl_snr_avg", "phy_dl_snr_max", "phy_rsrp_avg", "phy_pathloss_avg",
       "phy_dl_brate_peak_mbps", "phy_dl_brate_avg_mbps",
       "phy_ul_brate_peak_mbps", "phy_ul_brate_avg_mbps",
       "phy_dl_bler_avg", "phy_ul_bler_avg", "phy_dl_turbo_avg",
       "phy_cfo_avg", "phy_ul_ta_avg", "phy_sample_count"]
    + ["gnb_dl_brate_peak_mbps", "gnb_dl_brate_avg_mbps",
       "gnb_ul_brate_peak_mbps", "gnb_ul_brate_avg_mbps",
       "gnb_sys_load_avg", "gnb_cpu_avg_pct", "gnb_proc_rmem_kb_avg",
       "gnb_thread_count", "gnb_sample_count"]
    + ["gnbj_dl_cqi_avg", "gnbj_dl_mcs_avg", "gnbj_dl_mcs_max",
       "gnbj_ul_mcs_avg", "gnbj_ul_mcs_max", "gnbj_ul_snr_avg",
       "gnbj_ul_pusch_rssi_avg", "gnbj_ul_pucch_rssi_avg", "gnbj_ul_pucch_ni_avg",
       "gnbj_dl_bler_avg", "gnbj_ul_bler_avg",
       "gnbj_dl_bitrate_avg_mbps", "gnbj_ul_bitrate_avg_mbps",
       "gnbj_ul_phr_avg", "gnbj_dl_total_bytes", "gnbj_ul_total_bytes",
       "gnbj_dl_latency_avg_ms", "gnbj_sample_count"]
)


def run_single_ue_measurements(n, ue_ip, attach_ms):
    """Executes full ping, 36 iperf rates, RAN/power metrics for UE n."""
    row = {"ver": "v23_11", "ue_id": n, "attach_ok": "OK", "attach_ms": attach_ms, "ue_ip": ue_ip}
    
    # Ping
    print(f"  Ping...")
    p_avg, p_min, p_max, p_mdev, p_loss = run_ping(n)
    print(f"  ping avg={p_avg}ms loss={p_loss}%")
    row.update({
        "ping_avg_ms": p_avg, "ping_min_ms": p_min,
        "ping_max_ms": p_max, "ping_jitter_ms": p_mdev, "ping_loss_pct": p_loss
    })

    if p_loss == 100.0:
        recovered, new_ip, new_ms = heal_upf(n)
        if recovered:
            p_avg, p_min, p_max, p_mdev, p_loss = run_ping(n)
            row.update({
                "ue_ip": new_ip, "attach_ms": new_ms,
                "ping_avg_ms": p_avg, "ping_min_ms": p_min,
                "ping_max_ms": p_max, "ping_jitter_ms": p_mdev, "ping_loss_pct": p_loss
            })

    # DL iperf
    print("  DL iperf...")
    for rate in DL_RATES:
        mbps, loss = run_iperf(n, rate, "dl")
        row[f"dl_{rate}m_mbps"]     = mbps
        row[f"dl_{rate}m_loss_pct"] = loss
        print(f"    DL {rate:2d}M → {mbps:6.3f} Mbps  loss={loss}%")

    # UL iperf + background RAN telemetry during 2M UL
    ran_holder = [{}]
    print("  UL iperf...")
    for idx_r, rate in enumerate(UL_RATES):
        snap_thread = None
        if idx_r == 1:
            def _snap_all():
                time.sleep(2)
                ran_holder[0] = snap_gnb_ran(n)
            snap_thread = threading.Thread(target=_snap_all, daemon=True)
            snap_thread.start()

        mbps, loss = run_iperf(n, rate, "ul")
        row[f"ul_{rate}m_mbps"]     = mbps
        row[f"ul_{rate}m_loss_pct"] = loss
        print(f"    UL {rate:2d}M → {mbps:6.3f} Mbps  loss={loss}%")

        if snap_thread and snap_thread.is_alive():
            snap_thread.join(timeout=8)

    ran = ran_holder[0] or snap_gnb_ran(n)
    print(f"  gNB RAN params...")
    print(f"  dl_mcs={ran.get('dl_mcs')}  ul_mcs={ran.get('ul_mcs')}  "
          f"cqi={ran.get('cqi')}  sinr={ran.get('pusch_sinr')}dB")
    for k, v in ran.items():
        row[f"ran_{k}"] = v

    # Hardware & gNB/UE telemetry
    print("  gNB power snapshot (turbostat)...")
    pwr = snap_gnb_metrics()
    print(f"  pkg={pwr.get('ts_pkg_watt')}W(ts)  rapl={pwr.get('rapl_total_w')}W  "
          f"busy={pwr.get('ts_busy_pct')}%  load={pwr.get('load1')}/{pwr.get('load5')}  "
          f"CoreTmp={pwr.get('ts_core_tmp')}°C")
    for k, v in pwr.items():
        row[f"gnb_{k}"] = v

    g_agg = snap_gnb_agg(n)
    print(f"  gnb_agg: dl_peak={g_agg.get('dl_brate_peak_mbps')}Mbps  n_samples={g_agg.get('sample_count')}")
    for k, v in g_agg.items():
        row[f"gnb_{k}"] = v

    gj = snap_gnbj(n)
    print(f"  gnbj: cqi={gj.get('dl_cqi_avg')}  dl_mcs={gj.get('dl_mcs_avg')}  n_samples={gj.get('sample_count')}")
    for k, v in gj.items():
        row[f"gnbj_{k}"] = v

    phy = snap_ue_phy(n)
    print(f"  phy: dl_mcs_avg={phy.get('dl_mcs_avg')}  dl_snr_avg={phy.get('dl_snr_avg')}  rsrp={phy.get('rsrp_avg')}")
    for k, v in phy.items():
        row[f"phy_{k}"] = v

    return row


def main():
    print(f"\n{'#'*62}")
    print(f"  v23.11  Rerun Failed UEs with 120s Timeout & Fast-Connect")
    print(f"  gnb1=pc802  uehost1=pc801  core=pc808")
    print(f"  Output: {OUT_FILE} and {DESK_FILE}")
    print(f"{'#'*62}\n")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Inspect existing CSV
    good_dict = {}
    if OUT_FILE.exists() and OUT_FILE.stat().st_size > 0:
        with open(OUT_FILE, newline="") as fh:
            reader = csv.DictReader(fh)
            for r in reader:
                ue = int(r.get("ue_id", 0))
                try: ploss = float(r.get("ping_loss_pct", 100))
                except ValueError: ploss = 100.0
                attach_ok = r.get("attach_ok", "") == "OK"
                if attach_ok and ploss < 100.0:
                    good_dict[ue] = r

    failed_ues = [ue for ue in range(1, MAX_UE + 1) if ue not in good_dict]
    print(f"  Found {len(good_dict)} good UEs in CSV: {sorted(list(good_dict.keys()))}")
    print(f"  Found {len(failed_ues)} failed UEs to redo: {failed_ues}")

    # 2. Kill all, restart EPC fresh
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
        loop = (
            f"while true; do "
            f"  /usr/bin/iperf3 -s -p {port} --one-off -B 0.0.0.0 >/dev/null 2>&1; "
            f"  sleep 0.1; "
            f"done"
        )
        ssh_bg(CORE_HOST, loop)

    # Dictionary storing final row results for all 50 UEs
    final_rows = {}
    n_attached = 0

    def sync_to_disk():
        # Write sorted rows to OUT_FILE and DESK_FILE
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

    # 3. Accumulate sequentially 1..50
    for n in range(1, MAX_UE + 1):
        is_failed_ue = n in failed_ues
        print(f"\n{'='*62}")
        print(f"  UE {n:2d}/50  [{time.strftime('%H:%M:%S')}] — {'[REDO FULL MEASUREMENT]' if is_failed_ue else '[FAST CONNECT]'}")
        print(f"  Active UEs running: {n_attached}")
        print(f"{'='*62}")

        # Start gNB & UE
        print(f"  Starting srsenb {n}...")
        start_gnb_ue(n)
        if not wait_gnb_port(n, timeout=20):
            print(f"  PORT_FAIL — srsenb {n} did not bind")
            if is_failed_ue:
                row = {"ver": "v23_11", "ue_id": n, "n_attached": n_attached, "attach_ok": "FAIL_GNB", "attach_ms": 0, "ue_ip": ""}
                for fld in FIELDS: row.setdefault(fld, 0.0 if "mbps" in fld or "pct" in fld else "")
                final_rows[n] = row
                sync_to_disk()
            continue

        print(f"  Starting srsue {n}...")
        start_srsue(n)

        # Wait attach (timeout=120s)
        print(f"  Waiting for attach (timeout={ATTACH_TIMEOUT}s)...")
        ue_ip, attach_ms = wait_attach(n, timeout=ATTACH_TIMEOUT)

        if not ue_ip:
            print(f"  ATTACH FAILED — UE{n} ({attach_ms}ms)")
            if is_failed_ue:
                row = {"ver": "v23_11", "ue_id": n, "n_attached": n_attached, "attach_ok": "FAIL", "attach_ms": attach_ms, "ue_ip": "", "ping_avg_ms": 999.0, "ping_loss_pct": 100.0}
                for fld in FIELDS: row.setdefault(fld, 0.0 if "mbps" in fld or "loss" in fld else "")
                final_rows[n] = row
                sync_to_disk()
            continue

        n_attached += 1
        print(f"  Attached ✓  IP={ue_ip}  {attach_ms}ms  active={n_attached}")
        inject_default_route(n)
        time.sleep(PING_SETTLE_S)

        if is_failed_ue:
            # Measure fully
            measured_row = run_single_ue_measurements(n, ue_ip, attach_ms)
            measured_row["n_attached"] = n_attached
            for fld in FIELDS: measured_row.setdefault(fld, "")
            final_rows[n] = measured_row
            sync_to_disk()
            print(f"  ✓ UE{n} fully measured & saved to CSV/Desktop.")
        else:
            # Use existing good row data, update active count
            row = dict(good_dict[n])
            row["n_attached"] = n_attached
            row["attach_ms"] = attach_ms
            row["ue_ip"] = ue_ip
            final_rows[n] = row
            sync_to_disk()
            print(f"  ✓ UE{n} fast-connected & existing data retained.")

    print(f"\n{'#'*62}")
    print(f"  v23.11 Rerun Complete! Output: {OUT_FILE}")
    print(f"{'#'*62}")

if __name__ == "__main__":
    main()
