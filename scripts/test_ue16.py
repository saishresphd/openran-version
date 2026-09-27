#!/usr/bin/env python3
"""
test_ue16.py — Quick sanity check: attach UE16 on top of UEs 1-15,
test ping to core (10.45.0.1) and iperf3 DL+UL at 1M and 5M.
Prints pass/fail for each step.
Run from: /Users/saishurumkar/.bob/playground
"""
import subprocess, time, re, json, os

KEY      = os.path.expanduser("~/.ssh/id_ed25519")
GNB_HOST = "saish@pc802.emulab.net"
UE_HOST  = "saish@pc801.emulab.net"
CORE_HOST= "saish@pc808.emulab.net"
CORE_IP  = "10.45.0.1"
SRSENB   = "/opt/srsRAN_src/build/srsenb/src/srsenb"
SRSUE    = "/opt/srsRAN_src/build/srsue/src/srsue"
ENB_CONF = "/etc/srsenb_v22/enb_ue16.conf"
UE_CONF  = "/etc/srsue_v22/ue16.conf"
GNB_LOG  = "/tmp/gnb_logs_v23/enb_ue16.log"
UE_LOG   = "/tmp/ue_logs_v23/ue16.log"
UE_CSV   = "/tmp/ue_logs_v23/ue16_metrics.csv"
MET_CSV  = "/tmp/gnb_logs_v23/enb_ue16_metrics.csv"
RPT_JSON = "/tmp/gnb_logs_v23/enb_ue16_report.json"
CTX_DIR  = "/tmp/ue_ctx_v23/ue16"
IPERF_PORT = 5216

SSH_ARGS = ["-i", KEY, "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=15", "-o", "BatchMode=yes"]

def ssh(host, cmd, timeout=30):
    r = subprocess.run(["ssh"] + SSH_ARGS + [host, "bash -s"],
                       input=cmd, capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip()

def ssh_bg(host, cmd):
    import hashlib
    tag = hashlib.md5(cmd.encode()).hexdigest()[:8]
    rscript = f"/tmp/bg_{tag}.sh"
    subprocess.run(["ssh"] + SSH_ARGS + [host, f"cat > {rscript} && chmod +x {rscript}"],
                   input=f"#!/bin/bash\n{cmd}\n", capture_output=True, text=True, timeout=12)
    launcher = f"setsid bash {rscript} </dev/null >/dev/null 2>&1 & disown $! && exit 0"
    subprocess.run(["ssh"] + SSH_ARGS + [host, f"bash -c '{launcher}'"],
                   capture_output=True, text=True, timeout=12)
    time.sleep(0.5)

def ok(msg):  print(f"  ✓  {msg}")
def fail(msg):print(f"  ✗  {msg}")
def info(msg):print(f"  →  {msg}")

print("\n" + "="*60)
print("  TEST: UE16 attach + ping + iperf  (UEs 1-15 must be up)")
print("="*60)

# ── Step 0: confirm UEs 1-15 are attached ─────────────────────
print("\n[0] Checking UEs 1-15 are attached...")
out = ssh(UE_HOST,
    "for n in $(seq 1 15); do "
    "IP=$(sudo ip netns exec ue$n ip -br a 2>/dev/null | grep tun | awk '{print $3}'); "
    "[ -n \"$IP\" ] && echo \"ok:$n:$IP\" || echo \"miss:$n\"; done")
attached = []
for line in out.splitlines():
    if line.startswith("ok:"):
        parts = line.split(":")
        attached.append(int(parts[1]))
    else:
        info(f"UE{line.split(':')[1]} not attached")
info(f"UEs attached: {attached}")
if len(attached) < 15:
    fail(f"Only {len(attached)}/15 UEs attached — Phase 1 may not have completed")
else:
    ok("All 15 UEs attached")

# ── Step 1: kill any stale UE16 processes ─────────────────────
print("\n[1] Killing any stale UE16 gNB/srsue...")
ssh(GNB_HOST, f"sudo pkill -9 -f 'srsenb.*enb_ue16\\.conf' 2>/dev/null; sleep 1; echo done")
ssh(UE_HOST,  f"sudo pkill -9 -f 'srsue.*ue16\\.conf'      2>/dev/null; sleep 1; echo done")
ssh(GNB_HOST, f"rm -f {GNB_LOG} {MET_CSV} {RPT_JSON} 2>/dev/null; echo done")
ssh(UE_HOST,  f"rm -f {UE_LOG} {UE_CSV} 2>/dev/null; echo done")
ok("stale processes cleared")

# ── Step 2: start gNB slot 16 ─────────────────────────────────
print("\n[2] Starting srsenb for UE16...")
ssh_bg(GNB_HOST,
    f"sudo {SRSENB} {ENB_CONF}"
    f" --expert.metrics_csv_enable=1"
    f" --expert.metrics_csv_filename={MET_CSV}"
    f" --expert.metrics_period_secs=1"
    f" --expert.report_json_enable=1"
    f" --expert.report_json_filename={RPT_JSON}"
    f" >> {GNB_LOG} 2>&1")

# wait for ZMQ port 40160
print("   Waiting for gNB port 40160...")
port_up = False
for _ in range(15):
    time.sleep(2)
    out = ssh(GNB_HOST, "ss -tnlp 2>/dev/null | grep -c ':40160 ' || echo 0")
    if out.strip() == "1":
        port_up = True
        break
if port_up:
    ok("gNB port 40160 bound")
else:
    fail("gNB port 40160 NOT bound — check gNB log")
    out = ssh(GNB_HOST, f"tail -20 {GNB_LOG} 2>/dev/null")
    print(out)
    raise SystemExit(1)

# ── Step 3: start srsue 16 ────────────────────────────────────
print("\n[3] Starting srsue 16...")
ssh(UE_HOST,
    f"mkdir -p /tmp/ue_logs_v23 {CTX_DIR} && "
    f"rm -f {CTX_DIR}/.ctxt /users/saish/.ctxt /root/.ctxt 2>/dev/null; true")
ssh_bg(UE_HOST,
    f"cd {CTX_DIR} && "
    f"sudo HOME={CTX_DIR} {SRSUE} {UE_CONF}"
    f" --general.metrics_csv_enable=1"
    f" --general.metrics_csv_filename={UE_CSV}"
    f" --general.metrics_period_secs=1"
    f" >> {UE_LOG} 2>&1")

# ── Step 4: wait for attach ────────────────────────────────────
print("\n[4] Waiting for UE16 attach (up to 90s)...")
t0 = time.time()
ue_ip = None
time.sleep(6)
for _ in range(28):
    time.sleep(3)
    out = ssh(UE_HOST,
        "sudo ip netns exec ue16 ip -br a 2>/dev/null | grep -i tun | awk '{print $1,$3}'")
    parts = out.strip().split()
    if len(parts) >= 2:
        tun16  = parts[0]
        ue_ip  = parts[1].split("/")[0]
        break
attach_ms = int((time.time()-t0)*1000)

if not ue_ip:
    fail(f"Attach FAILED after {attach_ms}ms")
    out = ssh(UE_HOST, f"tail -20 {UE_LOG} 2>/dev/null")
    print(out)
    raise SystemExit(1)

ok(f"Attached  IP={ue_ip}  tun={tun16}  time={attach_ms}ms")

# Inject default route via actual tun name (may be tun_ue16 or tun_srsue16)
chk = ssh(UE_HOST, "sudo ip netns exec ue16 ip route show default 2>/dev/null")
if "default" not in chk:
    ssh(UE_HOST,
        f"sudo ip netns exec ue16 ip route add default dev {tun16} 2>/dev/null || true")
    ok(f"Default route injected via {tun16}")
else:
    ok(f"Default route already present: {chk.strip()}")

# Sanity: confirm IP is NOT .2 through .16 (would be a collision)
octet = int(ue_ip.split(".")[-1])
if 2 <= octet <= 16:
    fail(f"IP collision! Got {ue_ip} — overlaps with UEs 1-15 range (.2-.16).")
else:
    ok(f"IP {ue_ip} is clean (no collision with UEs 1-15)")

time.sleep(3)   # settle after route injection

# ── Step 5: ping test ─────────────────────────────────────────
print(f"\n[5] Ping test from ue16 netns → {CORE_IP} (20 packets)...")
out = ssh(UE_HOST,
    f"sudo ip netns exec ue16 ping -c 20 -i 0.3 -W 2 {CORE_IP}",
    timeout=50)
loss_m = re.search(r'(\d+(?:\.\d+)?)% packet loss', out)
rtt_m  = re.search(r'rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)', out)
loss   = float(loss_m.group(1)) if loss_m else 100.0
p_avg  = float(rtt_m.group(2))  if rtt_m  else 999.0

if loss >= 50.0:
    # Check UPF log for spoofing drops
    upf_log = ssh(CORE_HOST,
        "sudo journalctl -u open5gs-upfd --since '1 minute ago' --no-pager 2>/dev/null"
        " | grep -c 'Spoofing' || echo 0")
    info(f"UPF spoofing drops in last 1 min: {upf_log.strip()}")
    if int(upf_log.strip() or 0) > 0:
        info("Stale PFCP/TEID detected — restarting open5gs-upfd only (no SMF/MME touch)...")
        # Kill UE16 srsue+gNB first
        ssh(UE_HOST,
            "sudo pkill -TERM -f 'srsue.*ue16\\.conf' 2>/dev/null; sleep 3; "
            "sudo pkill -9 -f 'srsue.*ue16\\.conf' 2>/dev/null; sleep 1; echo done")
        ssh(GNB_HOST,
            "sudo pkill -9 -f 'srsenb.*enb_ue16\\.conf' 2>/dev/null; sleep 1; echo done")
        # Restart only UPF
        upf_status = ssh(CORE_HOST,
            "sudo systemctl restart open5gs-upfd && sleep 5\n"
            "echo upfd=$(systemctl is-active open5gs-upfd)", timeout=20)
        ok(f"UPF restarted: {upf_status.strip()}")
        info("Waiting 15s for UEs 1-15 to re-register PFCP sessions with fresh UPF...")
        time.sleep(15)
        # Re-attach UE16
        info("Re-attaching UE16...")
        ssh(GNB_HOST,
            f"rm -f {GNB_LOG} {MET_CSV} {RPT_JSON} 2>/dev/null; echo done")
        ssh_bg(GNB_HOST,
            f"sudo {SRSENB} {ENB_CONF}"
            f" --expert.metrics_csv_enable=1 --expert.metrics_csv_filename={MET_CSV}"
            f" --expert.metrics_period_secs=1 --expert.report_json_enable=1"
            f" --expert.report_json_filename={RPT_JSON} >> {GNB_LOG} 2>&1")
        # wait port
        port_up2 = False
        for _ in range(15):
            time.sleep(2)
            chk = ssh(GNB_HOST, "ss -tnlp 2>/dev/null | grep -c ':40160 ' || echo 0")
            if chk.strip() == "1":
                port_up2 = True; break
        if not port_up2:
            fail("gNB port 40160 not bound after UPF restart"); raise SystemExit(1)
        ssh(UE_HOST,
            f"rm -f {CTX_DIR}/.ctxt /users/saish/.ctxt /root/.ctxt 2>/dev/null; "
            f"sudo ip netns exec ue16 ip link set lo up 2>/dev/null; true")
        ssh_bg(UE_HOST,
            f"cd {CTX_DIR} && sudo HOME={CTX_DIR} {SRSUE} {UE_CONF}"
            f" --general.metrics_csv_enable=1 --general.metrics_csv_filename={UE_CSV}"
            f" --general.metrics_period_secs=1 >> {UE_LOG} 2>&1")
        time.sleep(6)
        for _ in range(25):
            time.sleep(3)
            chk2 = ssh(UE_HOST,
                "sudo ip netns exec ue16 ip -br a 2>/dev/null | grep tun | awk '{print $3}'")
            if chk2.strip():
                ue_ip = chk2.strip().split("/")[0]
                ok(f"Re-attached: IP={ue_ip}")
                break
        # Re-run ping
        out = ssh(UE_HOST,
            f"sudo ip netns exec ue16 ping -c 20 -i 0.3 -W 2 {CORE_IP}", timeout=50)
        loss_m = re.search(r'(\d+(?:\.\d+)?)% packet loss', out)
        rtt_m  = re.search(r'rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)', out)
        loss   = float(loss_m.group(1)) if loss_m else 100.0
        p_avg  = float(rtt_m.group(2))  if rtt_m  else 999.0

if loss < 5.0:
    ok(f"Ping OK  avg={p_avg}ms  loss={loss}%")
else:
    fail(f"Ping FAILED  avg={p_avg}ms  loss={loss}%")
    print(out[-300:])

# ── Step 6: iperf3 server on core ────────────────────────────
print(f"\n[6] Starting iperf3 server on core (port {IPERF_PORT})...")
ssh(CORE_HOST, f"sudo pkill -f 'iperf3.*{IPERF_PORT}' 2>/dev/null; sleep 0.5; echo done")
loop = (f"while true; do "
        f"iperf3 -s -B {CORE_IP} -p {IPERF_PORT} --one-off 2>/dev/null; "
        f"sleep 0.2; done")
ssh_bg(CORE_HOST, loop)
time.sleep(2)
out = ssh(CORE_HOST, f"ss -tnlp | grep -c ':{IPERF_PORT} ' || echo 0")
if out.strip() == "1":
    ok(f"iperf3 server listening on port {IPERF_PORT}")
else:
    fail(f"iperf3 server NOT listening on port {IPERF_PORT}")

# ── Step 7: DL iperf 1M ──────────────────────────────────────
print(f"\n[7] DL iperf  1M (10s)...")
def run_iperf(rate, direction):
    flag = "-R" if direction == "dl" else ""
    cmd  = (f"sudo ip netns exec ue16 iperf3 -c {CORE_IP} -p {IPERF_PORT} "
            f"-b {rate}M -t 10 {flag} --json")
    out = ssh(UE_HOST, cmd, timeout=30)
    try:
        dec = json.JSONDecoder(); last = None; i = 0
        while i < len(out):
            try:
                obj, idx = dec.raw_decode(out, i)
                if isinstance(obj, dict) and "end" in obj:
                    last = obj
                i = idx
            except json.JSONDecodeError:
                i += 1
        if not last or last.get("error"):
            return 0.0, out[-200:]
        key  = "sum_received" if direction == "dl" else "sum_sent"
        mbps = round(last["end"][key]["bits_per_second"]/1e6, 4)
        return mbps, ""
    except Exception as e:
        return 0.0, str(e)

mbps, err = run_iperf(1, "dl")
if mbps > 0.5:
    ok(f"DL  1M → {mbps} Mbps")
else:
    fail(f"DL  1M → {mbps} Mbps  err={err[:100]}")

time.sleep(2)

print(f"\n[8] DL iperf  5M (10s)...")
mbps, err = run_iperf(5, "dl")
if mbps > 0.5:
    ok(f"DL  5M → {mbps} Mbps")
else:
    fail(f"DL  5M → {mbps} Mbps  err={err[:100]}")

time.sleep(2)

print(f"\n[9] UL iperf  1M (10s)...")
mbps, err = run_iperf(1, "ul")
if mbps > 0.5:
    ok(f"UL  1M → {mbps} Mbps")
else:
    fail(f"UL  1M → {mbps} Mbps  err={err[:100]}")

time.sleep(2)

print(f"\n[10] UL iperf  5M (10s)...")
mbps, err = run_iperf(5, "ul")
if mbps > 0.5:
    ok(f"UL  5M → {mbps} Mbps")
else:
    fail(f"UL  5M → {mbps} Mbps  err={err[:100]}")

# ── Summary ───────────────────────────────────────────────────
print("\n" + "="*60)
print("  TEST COMPLETE")
print(f"  UE16 IP      : {ue_ip}")
print(f"  Attach time  : {attach_ms}ms")
print(f"  Ping loss    : {loss}%")
print("="*60)
print("\nIf all ✓ — safe to start full experiment (python3 scripts/run_v23_50ue.py)")
print("If any ✗ — check the error above before starting full run.\n")
