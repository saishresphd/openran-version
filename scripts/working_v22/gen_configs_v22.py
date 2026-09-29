#!/usr/bin/env python3
"""
gen_configs_v22.py — Generate and install 50 gNB (srsenb) + 50 UE (srsue) configs
for srsRAN v22.10 on POWDER nodes:
  core    = pc808   10.10.1.1
  gnb1    = pc802   10.10.1.2
  uehost1 = pc801   10.10.1.4
"""

import subprocess

def gtp_ip(i):
    return f"10.10.2.{i}"

def make_enb_conf(i):
    tx = 40000 + i * 10
    rx = 40000 + i * 10 + 1
    return f"""[enb]
enb_id = {hex(i)}
mcc = 999
mnc = 70
mme_addr = 10.10.1.1
gtp_bind_addr = {gtp_ip(i)}
s1c_bind_addr = {gtp_ip(i)}
s1c_bind_port = 0
n_prb = 50

[enb_files]
sib_config = /etc/srsran/sib.conf
rr_config  = /etc/srsran/rr.conf
rb_config  = /etc/srsran/rb.conf

[rf]
dl_earfcn = 3350
tx_gain   = 80
rx_gain   = 40
device_name = zmq
device_args = fail_on_disconnect=false,tx_port=tcp://*:{tx},rx_port=tcp://10.10.1.4:{rx},id=enb{i},base_srate=11.52e6

[log]
all_level  = warning
mac_level  = info
s1ap_level = info
filename   = /tmp/gnb_logs/enb_ue{i}.log
file_max_size = -1

[scheduler]
policy = time_pf

[expert]
nof_phy_threads = 2
rrc_inactivity_timer = 3600000
"""

def make_ue_conf(i):
    imsi = f"99970000000{i:04d}"
    imei = f"35349006987{i:04d}"[:15]
    tx = 40000 + i * 10 + 1
    rx = 40000 + i * 10
    return f"""[rat.eutra]
dl_earfcn = 3350

[usim]
mode = soft
algo = milenage
opc  = E8ED289DEBA952E4283B54E88E6183CA
k    = 465B5CE8B199B49FAA5F0A2EE238A6BC
imsi = {imsi}
imei = {imei}

[rrc]
ue_category = 4

[nas]
apn          = internet
apn_protocol = ipv4

[gw]
netns      = ue{i}
ip_devname = tun_ue{i}

[rf]
device_name = zmq
device_args = fail_on_disconnect=false,tx_port=tcp://*:{tx},rx_port=tcp://10.10.1.2:{rx},id=ue{i},base_srate=11.52e6
tx_gain     = 80
rx_gain     = 40

[log]
all_level = warning
nas_level = info
filename  = /tmp/ue_logs/ue{i}.log
file_max_size = -1
"""

def install():
    print("Generating v22.10 configs...")
    with open("/tmp/install_v22_gnb_confs.sh", "w") as f:
        f.write("sudo mkdir -p /etc/srsenb_v22 /tmp/gnb_logs\n")
        for i in range(1, 51):
            f.write(f'sudo tee /etc/srsenb_v22/enb_ue{i}.conf > /dev/null << "EOF"\n{make_enb_conf(i)}EOF\n')

    with open("/tmp/install_v22_ue_confs.sh", "w") as f:
        f.write("sudo mkdir -p /etc/srsue_v22 /tmp/ue_logs\n")
        for i in range(1, 51):
            f.write(f'sudo tee /etc/srsue_v22/ue{i}.conf > /dev/null << "EOF"\n{make_ue_conf(i)}EOF\n')

    print("Pushing to gnb1 (pc802)...")
    subprocess.run(["scp", "-o", "BatchMode=yes", "/tmp/install_v22_gnb_confs.sh", "saish@pc802.emulab.net:/tmp/install_confs.sh"], check=True)
    subprocess.run(["ssh", "-o", "BatchMode=yes", "saish@pc802.emulab.net", "bash /tmp/install_confs.sh"], check=True)

    print("Pushing to uehost1 (pc801)...")
    subprocess.run(["scp", "-o", "BatchMode=yes", "/tmp/install_v22_ue_confs.sh", "saish@pc801.emulab.net:/tmp/install_confs.sh"], check=True)
    subprocess.run(["ssh", "-o", "BatchMode=yes", "saish@pc801.emulab.net", "bash /tmp/install_confs.sh"], check=True)

    print("✓ v22.10 configs installed on pc802 and pc801 successfully!")

if __name__ == "__main__":
    install()
