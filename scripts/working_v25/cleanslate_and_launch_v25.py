#!/usr/bin/env python3
"""
cleanslate_and_launch_v25.py — Clean slate + launch run_v25_50ue.py.
"""
import subprocess, time, os, signal, csv, pathlib

KEY  = os.path.expanduser("~/.ssh/id_ed25519")
OPTS = ["-i", KEY,
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=15",
        "-o", "BatchMode=yes"]

def ssh(host, cmd, timeout=60):
    r = subprocess.run(["ssh"] + OPTS + [host, "bash -s"],
                       input=cmd, capture_output=True, text=True, timeout=timeout)
    out = r.stdout.strip()
    if out:
        print(out)
    return out

print("=" * 60)
print("  CLEAN SLATE  (v25.10)")
print("=" * 60)

# 0. Kill any running local experiment
print("\n[0] Killing local run_v2* processes...")
r = subprocess.run(["pgrep", "-f", "run_v2"], capture_output=True, text=True)
for pid in r.stdout.strip().splitlines():
    try:
        os.kill(int(pid), signal.SIGKILL)
        print(f"  killed local pid {pid}")
    except Exception:
        pass

# 1. Kill srsenb on gNB
print("\n[1/5] Killing all srsenb / gNB (pc802)...")
ssh("saish@pc802.emulab.net",
    "sudo killall srsenb gnb 2>/dev/null; sleep 2\n"
    "sudo killall -9 srsenb gnb 2>/dev/null; sleep 1\n"
    "echo gnb_procs=$(pgrep srsenb | wc -l | tr -d ' ')")

# 2. Kill srsue on UE host
print("\n[2/5] Killing all srsue / UE (pc801)...")
ssh("saish@pc801.emulab.net",
    "sudo killall srsue ue 2>/dev/null; sleep 3\n"
    "sudo killall -9 srsue ue 2>/dev/null; sleep 1\n"
    "echo ue_procs=$(pgrep srsue | wc -l | tr -d ' ')")

# 3. Kill iperf3 on core
print("\n[3/5] Killing iperf3 (pc808)...")
ssh("saish@pc808.emulab.net",
    "sudo pkill -9 iperf3 2>/dev/null; sleep 1\n"
    "echo iperf3_procs=$(pgrep iperf3 | wc -l | tr -d ' ')")

# 4. Full Core restart
print("\n[4/5] Full Core restart (pc808)...")
ssh("saish@pc808.emulab.net",
    "sudo systemctl restart open5gs-smfd   && sleep 3\n"
    "sudo systemctl restart open5gs-upfd   && sleep 2\n"
    "sudo systemctl restart open5gs-sgwud  && sleep 2\n"
    "sudo systemctl restart open5gs-sgwcd  && sleep 2\n"
    "sudo systemctl restart open5gs-mmed   && sleep 4\n"
    "echo epc=$(systemctl is-active open5gs-mmed open5gs-smfd "
    "open5gs-sgwcd open5gs-sgwud open5gs-upfd | tr '\\n' '/')",
    timeout=35)

# 5. Clean tun interfaces + NAS contexts on UE host
print("\n[5/5] Cleaning tun interfaces + NAS contexts (pc801)...")
ssh("saish@pc801.emulab.net",
    "for n in $(seq 1 50); do\n"
    "  sudo ip netns exec ue$n ip link delete tun_ue$n    2>/dev/null || true\n"
    "  sudo ip netns exec ue$n ip link delete tun_srsue$n 2>/dev/null || true\n"
    "  rm -f /tmp/ue_ctx/ue$n/.ctxt 2>/dev/null\n"
    "done\n"
    "rm -f /users/saish/.ctxt /root/.ctxt 2>/dev/null\n"
    "echo tun_cleaned",
    timeout=30)

# 6. Clean old log files
ssh("saish@pc802.emulab.net",
    "rm -f /tmp/gnb_logs_v25/enb_ue*.log "
    "/tmp/gnb_logs_v25/enb_ue*_metrics.csv "
    "/tmp/gnb_logs_v25/enb_ue*_report.json 2>/dev/null\n"
    "echo gnb_logs_cleaned")
ssh("saish@pc801.emulab.net",
    "rm -f /tmp/ue_logs_v25/ue*.log "
    "/tmp/ue_logs_v25/ue*_metrics.csv 2>/dev/null\n"
    "echo ue_logs_cleaned")

# 7. Wipe CSV
csv_path = pathlib.Path("results/ver_eval/v25_10/ue_results_50.csv")
csv_path.parent.mkdir(parents=True, exist_ok=True)
if csv_path.exists():
    csv_path.unlink()
    print(f"\n  CSV deleted — experiment will start fresh from UE1")

print("\n" + "=" * 60)
print("  CLEAN SLATE DONE — launching v25.10 experiment")
print("=" * 60)

open("/tmp/run_v25_50ue.log", "w").close()
exp = subprocess.Popen(
    ["python3", "-u", "scripts/working_v25/run_v25_50ue.py"],
    stdout=open("/tmp/run_v25_50ue.log", "a"),
    stderr=subprocess.STDOUT,
    cwd=str(pathlib.Path(__file__).parent.parent.parent)
)
print(f"\n  Experiment PID: {exp.pid}")
print("  Monitor: tail -f /tmp/run_v25_50ue.log")
