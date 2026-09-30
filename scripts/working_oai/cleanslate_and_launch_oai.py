#!/usr/bin/env python3
"""
cleanslate_and_launch_oai.py — Clean slate + launch run_oai_50ue.py on POWDER testbed.
"""
import subprocess, time, os, signal, pathlib

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
print("  CLEAN SLATE  (OAI 4G LTE)")
print("=" * 60)

# 0. Kill any running local experiment
print("\n[0] Killing local run_oai* processes...")
r = subprocess.run(["pgrep", "-f", "run_oai"], capture_output=True, text=True)
for pid in r.stdout.strip().splitlines():
    try:
        os.kill(int(pid), signal.SIGKILL)
        print(f"  killed local pid {pid}")
    except Exception:
        pass

# 1. Kill eNB on gNB host
print("\n[1/5] Killing all lte-softmodem / srsenb (pc802)...")
ssh("saish@pc802.emulab.net",
    "sudo killall lte-softmodem srsenb 2>/dev/null; sleep 2\n"
    "sudo killall -9 lte-softmodem srsenb 2>/dev/null; sleep 1\n"
    "echo gnb_procs=$(pgrep -E 'lte-softmodem|srsenb' | wc -l | tr -d ' ')")

# 2. Kill UE on UE host
print("\n[2/5] Killing all lte-uesoftmodem / srsue (pc801)...")
ssh("saish@pc801.emulab.net",
    "sudo killall lte-uesoftmodem srsue 2>/dev/null; sleep 3\n"
    "sudo killall -9 lte-uesoftmodem srsue 2>/dev/null; sleep 1\n"
    "echo ue_procs=$(pgrep -E 'lte-uesoftmodem|srsue' | wc -l | tr -d ' ')")

# 3. Kill iperf3 on core
print("\n[3/5] Killing iperf3 (pc808)...")
ssh("saish@pc808.emulab.net",
    "sudo pkill -9 iperf3 2>/dev/null; sleep 1\n"
    "echo iperf3_procs=$(pgrep iperf3 | wc -l | tr -d ' ')")

# 4. Full EPC restart
print("\n[4/5] Full EPC restart (pc808)...")
ssh("saish@pc808.emulab.net",
    "sudo systemctl restart open5gs-smfd   && sleep 3\n"
    "sudo systemctl restart open5gs-upfd   && sleep 2\n"
    "sudo systemctl restart open5gs-sgwud  && sleep 2\n"
    "sudo systemctl restart open5gs-sgwcd  && sleep 2\n"
    "sudo systemctl restart open5gs-mmed   && sleep 4\n"
    "echo epc=$(systemctl is-active open5gs-mmed open5gs-smfd "
    "open5gs-sgwcd open5gs-sgwud open5gs-upfd | tr '\\n' '/')",
    timeout=35)

# 5. Clean tun interfaces + netns on UE host
print("\n[5/5] Cleaning tun interfaces (pc801)...")
ssh("saish@pc801.emulab.net",
    "for n in $(seq 1 50); do\n"
    "  sudo ip netns exec ue$n ip link delete oaitun_ue$n 2>/dev/null || true\n"
    "  sudo ip netns exec ue$n ip link delete tun_ue$n     2>/dev/null || true\n"
    "done\n"
    "echo tun_cleaned",
    timeout=30)

# 6. Clean old log files
ssh("saish@pc802.emulab.net", "rm -rf /tmp/gnb_logs_oai && mkdir -p /tmp/gnb_logs_oai; echo gnb_logs_cleaned")
ssh("saish@pc801.emulab.net", "rm -rf /tmp/ue_logs_oai && mkdir -p /tmp/ue_logs_oai; echo ue_logs_cleaned")

# 7. Reset CSV
csv_path = pathlib.Path("results/ver_eval/oai/ue_results_50.csv")
csv_path.parent.mkdir(parents=True, exist_ok=True)
if csv_path.exists():
    csv_path.unlink()
    print("  Previous OAI CSV deleted — experiment will start fresh from UE1")

desk_path = pathlib.Path(os.path.expanduser("~/Desktop/oai_ue_results_50.csv"))
if desk_path.exists():
    desk_path.unlink()
    print("  Desktop CSV deleted")

print("\n" + "=" * 60)
print("  CLEAN SLATE DONE — launching OAI 4G LTE experiment")
print("=" * 60)

open("/tmp/run_oai_50ue.log", "w").close()
exp = subprocess.Popen(
    ["python3", "-u", "scripts/working_oai/run_oai_50ue.py"],
    stdout=open("/tmp/run_oai_50ue.log", "a"),
    stderr=subprocess.STDOUT,
    cwd=str(pathlib.Path(__file__).parent.parent.parent)
)
print(f"\n  OAI Experiment PID: {exp.pid}")
print("  Monitor: tail -f /tmp/run_oai_50ue.log")
