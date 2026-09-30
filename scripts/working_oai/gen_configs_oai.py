#!/usr/bin/env python3
"""
gen_configs_oai.py — Generate and install 50 eNB (lte-softmodem) + 50 UE (lte-uesoftmodem)
configs for OpenAirInterface (OAI) 4G LTE on POWDER testbed nodes:
  core    = pc808   10.10.1.1   Open5GS EPC (MME + SGW + UPF)
  gnb1    = pc802   10.10.1.2   OAI eNB
  uehost1 = pc801   10.10.1.4   OAI UE host

Channel emulation:
  RFsimulator over TCP sockets:
  eNB n: listens on port 40000 + n * 10
  UE n : connects to 10.10.1.2:(40000 + n * 10)
  Bandwidth: 50 PRB (10 MHz, N_RB_DL=50, earfcn=3350, Band 7) matching srsRAN.
"""

import subprocess, os, pathlib

KEY  = os.path.expanduser("~/.ssh/id_ed25519")
OPTS = ["-i", KEY,
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=15",
        "-o", "BatchMode=yes"]

def ssh(host, cmd, timeout=30):
    r = subprocess.run(["ssh"] + OPTS + [host, "bash -s"],
                       input=cmd, capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip()

def gtp_ip(i):
    return f"10.10.2.{i}"

def make_enb_conf(i):
    rfsim_port = 40000 + i * 10
    telnet_port = 9090 + i
    enb_id = 0xe00 + i
    return f"""eNBs =
(
 {{{{
    // Identification parameters:
    eNB_ID    =  {enb_id};
    cell_type =  "CELL_MACRO_ENB";
    eNB_name  =  "enb_ue{i}";

    // Tracking area code, PLMN:
    tracking_area_code  =  1;
    mobile_country_code_fdd =  999;
    mobile_network_code_fdd =  70;
    mnc_length = 2;

    // Physical parameters:
    component_carriers = (
      {{{{
        node_function             = "eNodeB_3GPP";
        node_timing               = "synch_to_ext_device";
        node_synch_ref            = 0;
        frame_type                = "FDD";
        tdd_config                = 3;
        tdd_config_s              = 0;
        prefix_type               = "NORMAL";
        eutra_band                = 7;
        downlink_frequency        = 2680000000L;
        uplink_frequency_offset   = -120000000;
        Nid_cell                  = {i % 504};
        N_RB_DL                   = 50;
        Nid_cell_mbsfn            = 0;
        nb_antenna_ports          = 1;
        nb_antennas_tx            = 1;
        nb_antennas_rx            = 1;
        tx_gain                   = 80;
        rx_gain                   = 40;
        prach_root                = 0;
        prach_config_index        = 0;
        prach_high_speed          = "DISABLE";
        prach_zero_correlation    = 1;
        prach_freq_offset         = 2;
        pucch_delta_shift         = 1;
        pucch_nRB_CQI             = 1;
        pucch_nCS_AN              = 0;
        pucch_n1_AN               = 0;
        pdsch_referenceSignalPower= -27;
        pdsch_p_b                 = 0;
        pusch_n_SB                = 1;
        pusch_enable64QAM         = "DISABLE";
        pusch_hoppingMode         = "interSubFrame";
        pusch_hoppingOffset       = 0;
        pusch_groupHoppingEnabled = "ENABLE";
        pusch_groupAssignmentPUSCH= 0;
        pusch_sequenceHoppingEnabled = "DISABLE";
        pusch_nDMRS1              = 0;
        phich_resource            = "ONE";
        phich_duration            = "NORMAL";
      }}}}
    );

    // MME parameters:
    mme_ip_address = (
      {{{{
        ipv4       = "10.10.1.1";
        ipv6       = "192:168:30::17";
        active     = "yes";
        preference = "ipv4";
      }}}}
    );

    // Network interfaces:
    NETWORK_INTERFACES =
    {{{{
        ENB_INTERFACE_NAME_FOR_S1_MME = "enp4s0f1";
        ENB_IPV4_ADDRESS_FOR_S1_MME   = "{gtp_ip(i)}/24";
        ENB_INTERFACE_NAME_FOR_S1U     = "enp4s0f1";
        ENB_IPV4_ADDRESS_FOR_S1U       = "{gtp_ip(i)}/24";
        ENB_PORT_FOR_S1U               = 2152;
    }}}};
  }}}}
);

rfsimulator: {{
    serveraddr = "server";
    serverport = {rfsim_port};
    options = ();
    modelname = "AWGN";
    IQfile = "/tmp/rfsim_iq_enb{i}";
}};

telnetsrv: {{
    listenaddr = "127.0.0.1";
    listenport = {telnet_port};
}};

log_config: {{
    global_log_level = "info";
    hw_log_level     = "info";
    phy_log_level    = "info";
    mac_log_level    = "info";
    rlc_log_level    = "info";
    pdcp_log_level   = "info";
    rrc_log_level    = "info";
}};
"""

def make_ue_conf(i):
    imsi = f"99970000000{i:04d}"
    imei = f"35349006987{i:04d}"[:15]
    rfsim_port = 40000 + i * 10
    return f"""uemi = (
  {{{{
    msin = "{imsi[5:]}";
    mcc  = "999";
    mnc  = "70";
    key  = "465B5CE8B199B49FAA5F0A2EE238A6BC";
    opc  = "E8ED289DEBA952E4283B54E88E6183CA";
    msisdn = "00000001";
    imei = "{imei}";
  }}}}
);

rfsimulator: {{
    serveraddr = "10.10.1.2";
    serverport = {rfsim_port};
    options = ();
    modelname = "AWGN";
    IQfile = "/tmp/rfsim_iq_ue{i}";
}};

log_config: {{
    global_log_level = "info";
    phy_log_level    = "info";
    mac_log_level    = "info";
    rlc_log_level    = "info";
    pdcp_log_level   = "info";
    rrc_log_level    = "info";
    nas_log_level    = "info";
}};
"""

def install():
    print("Generating OAI 4G LTE configs for 50 UEs & 50 eNBs...")
    os.makedirs("/tmp/oai_confs/enb", exist_ok=True)
    os.makedirs("/tmp/oai_confs/ue", exist_ok=True)

    with open("/tmp/install_oai_enb_confs.sh", "w") as f:
        f.write("sudo mkdir -p /etc/oai_enb /tmp/gnb_logs_oai\n")
        for i in range(1, 51):
            f.write(f'sudo tee /etc/oai_enb/enb_ue{i}.conf > /dev/null << "EOF"\n{make_enb_conf(i)}EOF\n')
        f.write('echo "Installed 50 OAI eNB configs in /etc/oai_enb"\n')

    with open("/tmp/install_oai_ue_confs.sh", "w") as f:
        f.write("sudo mkdir -p /etc/oai_ue /tmp/ue_logs_oai\n")
        for i in range(1, 51):
            f.write(f'sudo tee /etc/oai_ue/ue{i}.conf > /dev/null << "EOF"\n{make_ue_conf(i)}EOF\n')
        f.write('echo "Installed 50 OAI UE configs in /etc/oai_ue"\n')

    print("Deploying eNB configs to pc802...")
    with open("/tmp/install_oai_enb_confs.sh", "r") as f:
        out = ssh("saish@pc802.emulab.net", f.read())
        print(f"  {out}")

    print("Deploying UE configs to pc801...")
    with open("/tmp/install_oai_ue_confs.sh", "r") as f:
        out = ssh("saish@pc801.emulab.net", f.read())
        print(f"  {out}")

    print("Config generation & deployment finished.")

if __name__ == "__main__":
    install()
