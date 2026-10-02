#!/usr/bin/env python3
"""
rerun_failed_oai_ues.py — Retest any FAIL UEs in results/ver_eval/oai/ue_results_50.csv
Runs individual failed UEs without disturbing existing successful records.
"""

import subprocess, time, csv, os, re, json, pathlib, shutil, sys

KEY       = os.path.expanduser("~/.ssh/id_ed25519")
GNB_HOST  = "saish@pc802.emulab.net"
UE_HOST   = "saish@pc801.emulab.net"
CORE_HOST = "saish@pc808.emulab.net"

OAI_ENB_BIN  = "/opt/openairinterface5g/cmake_targets/ran_build/build/lte-softmodem"
OAI_UE_BIN   = "/opt/openairinterface5g/cmake_targets/ran_build/build/lte-uesoftmodem"
ENB_CONF_DIR = "/etc/oai_enb"
UE_LOG_DIR   = "/tmp/ue_logs_oai"
GNB_LOG_DIR  = "/tmp/gnb_logs_oai"

ATTACH_TIMEOUT = 120
PING_COUNT     = 20
PING_SETTLE_S  = 5
DL_RATES = list(range(1, 11)) + list(range(15, 51, 5))
UL_RATES = list(range(1, 11)) + list(range(15, 51, 5))
IPERF_DUR      = 10
CORE_IP        = "10.45.0.1"

OUT_FILE  = pathlib.Path("results/ver_eval/oai/ue_results_50.csv")
DESK_FILE = pathlib.Path(os.path.expanduser("~/Desktop/oai_ue_results_50.csv"))

SSH_ARGS = ["-i", KEY, "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes"]

def ssh(host, cmd, timeout=30):
    r = subprocess.run(["ssh"] + SSH_ARGS + [host, "bash -s"], input=cmd, capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip(), r.returncode

def ssh_bg(host, cmd):
    import hashlib
    tag = hashlib.md5(cmd.encode()).hexdigest()[:8]
    rscript = f"/tmp/bg_{tag}.sh"
    subprocess.run(["ssh"] + SSH_ARGS + [host, f"cat > {rscript} && chmod +x {rscript}"], input=f"#!/bin/bash\n{cmd}\n", capture_output=True, text=True, timeout=30)
    subprocess.run(["ssh"] + SSH_ARGS + [host, f"bash -c 'setsid bash {rscript} </dev/null >/dev/null 2>&1 & disown $! && exit 0'"], capture_output=True, timeout=30)

def start_oai_enb(n):
    port = 40000 + n * 10
    conf = f"{ENB_CONF_DIR}/enb_ue{n}.conf"
    log  = f"{GNB_LOG_DIR}/enb_ue{n}.log"
    ssh(GNB_HOST, f"sudo mkdir -p {GNB_LOG_DIR} && sudo chmod 777 {GNB_LOG_DIR} && sudo rm -f {log}", timeout=10)
    cmd = f"sudo {OAI_ENB_BIN} -O {conf} --rfsim --rfsimulator.[0].serveraddr server --rfsimulator.[0].serverport {port} >> {log} 2>&1"
    ssh_bg(GNB_HOST, cmd)

def wait_enb_port(n, timeout=30):
    port = 40000 + n * 10
    deadline = time.time() + timeout
    while time.time() < deadline:
        out, _ = ssh(GNB_HOST, f"sudo ss -tnlp 2>/dev/null | grep -c ':{port} ' || echo 0", timeout=10)
        if out.strip() in ("1", "2"):
            return True
        time.sleep(2)
    return False

def start_oai_ue(n):
    port = 40000 + n * 10
    log  = f"{UE_LOG_DIR}/ue{n}.log"
    ctx_dir = f"/tmp/oai_ue_ctx/ue{n}"
    ssh(UE_HOST, f"sudo mkdir -p {UE_LOG_DIR} {ctx_dir} && sudo chmod 777 {UE_LOG_DIR} {ctx_dir} && sudo rm -f {log}", timeout=10)
    cmd = (f"cd {ctx_dir} && "
           f"sudo ip netns exec ue{n} {OAI_UE_BIN} "
           f"--rfsim --rfsimulator.[0].serveraddr 10.10.1.2 --rfsimulator.[0].serverport {port} "
           f"-r 50 -C 2680000000 --ue-rxgain 115 --ue-txgain 90 >> {log} 2>&1")
    ssh_bg(UE_HOST, cmd)

def wait_attach(n, timeout=ATTACH_TIMEOUT):
    t_start = time.time()
    deadline = t_start + timeout
    time.sleep(6)
    while time.time() < deadline:
        out, _ = ssh(UE_HOST, f"sudo ip netns exec ue{n} ip -br a 2>/dev/null | grep -E 'oaitun|tun|ue' | awk '{{print $3}}'", timeout=12)
        ip = out.strip().split("/")[0] if out.strip() else None
        if ip and ip.startswith("10.45.0."):
            return ip, int((time.time() - t_start) * 1000)
        time.sleep(3)
    return None, int(timeout * 1000)

def inject_default_route(n):
    cmd = (
        f"DEV=$(sudo ip netns exec ue{n} ip -br a 2>/dev/null | grep -E 'oaitun_ue[0-9]+' | awk '{{print $1}}' | head -1); "
        f"if [ -n \"$DEV\" ]; then "
        f"  sudo ip netns exec ue{n} ip route add 10.45.0.1 dev $DEV 2>/dev/null || true; "
        f"fi"
    )
    ssh(UE_HOST, cmd, timeout=12)

def run_ping(n, count=None):
    c = count or PING_COUNT
    cmd = f"sudo ip netns exec ue{n} ping -c {c} -i 0.3 -W 2 {CORE_IP}"
    out, _ = ssh(UE_HOST, cmd, timeout=c * 2 + 12)
    try:
        loss_m = re.search(r'(\d+(?:\.\d+)?)% packet loss', out)
        loss = float(loss_m.group(1)) if loss_m else 100.0
        rtt_m = re.search(r'rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)', out)
        if rtt_m:
            return float(rtt_m.group(2)), float(rtt_m.group(1)), float(rtt_m.group(3)), float(rtt_m.group(4)), loss
    except Exception:
        pass
    return 999.0, 999.0, 999.0, 999.0, 100.0

def run_iperf(n, ue_ip, rate_mbps, direction="dl"):
    port = 5200 + n
    flag = "-R" if direction == "dl" else ""
    cmd = f"sudo ip netns exec ue{n} iperf3 -c {CORE_IP} -B {ue_ip} -p {port} -b {rate_mbps}M -t {IPERF_DUR} {flag} --json"
    out, _ = ssh(UE_HOST, cmd, timeout=IPERF_DUR + 15)
    if out == "TIMEOUT": return 0.0, 100.0
    try:
        dec = json.JSONDecoder()
        last_d = None
        i = 0
        while i < len(out):
            try:
                obj, idx = dec.raw_decode(out, i); last_d = obj; i = idx
            except json.JSONDecodeError:
                i += 1
        if last_d is None or last_d.get("error"): return 0.0, 100.0
        end = last_d["end"]
        key = "sum_received" if direction == "dl" else "sum_sent"
        return round(end[key]["bits_per_second"] / 1e6, 4), 0.0
    except Exception:
        return 0.0, 100.0

def retest_ue(n):
    print(f"\n[RETEST] Starting UE {n}...")
    start_oai_enb(n)
    if not wait_enb_port(n):
        print(f"  eNB port {40000+n*10} not bound")
        return None
    start_oai_ue(n)
    ue_ip, attach_ms = wait_attach(n)
    if not ue_ip:
        print(f"  UE {n} attach failed")
        return None
    print(f"  UE {n} attached: {ue_ip} in {attach_ms}ms")
    inject_default_route(n)
    time.sleep(PING_SETTLE_S)
    p_avg, p_min, p_max, p_jit, p_loss = run_ping(n)
    
    dl_res = {}
    for r in DL_RATES:
        mbps, loss = run_iperf(n, ue_ip, r, "dl")
        dl_res[f"dl_{r}m_mbps"] = mbps
        dl_res[f"dl_{r}m_loss_pct"] = loss
        
    ul_res = {}
    for r in UL_RATES:
        mbps, loss = run_iperf(n, ue_ip, r, "ul")
        ul_res[f"ul_{r}m_mbps"] = mbps
        ul_res[f"ul_{r}m_loss_pct"] = loss
        
    return {
        "attach_ok": "OK",
        "attach_ms": attach_ms,
        "ue_ip": ue_ip,
        "ping_avg_ms": p_avg,
        "ping_min_ms": p_min,
        "ping_max_ms": p_max,
        "ping_jitter_ms": p_jit,
        "ping_loss_pct": p_loss,
        **dl_res,
        **ul_res
    }

def main():
    if not OUT_FILE.exists():
        print("CSV not found.")
        return
        
    rows = []
    with open(OUT_FILE, "r") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames
        rows = list(reader)
        
    failed_ues = [int(r["ue_id"]) for r in rows if r.get("attach_ok") != "OK"]
    if not failed_ues:
        print("No failed UEs to retest.")
        return
        
    print(f"Failed UEs to retest: {failed_ues}")
    for n in failed_ues:
        res = retest_ue(n)
        if res:
            for r in rows:
                if int(r["ue_id"]) == n:
                    r.update(res)
                    
    with open(OUT_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    try:
        shutil.copyfile(str(OUT_FILE), str(DESK_FILE))
    except Exception:
        pass
    print("Retest complete and CSV updated.")

if __name__ == "__main__":
    main()
