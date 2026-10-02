#!/usr/bin/env python3
"""
gen_configs_oai.py — Generate and install 50 eNB (lte-softmodem) + 50 UE NVRAM
configs for OpenAirInterface (OAI) 4G LTE on POWDER testbed nodes:
  core    = pc808   10.10.1.1   Open5GS EPC (MME + SGW + UPF)
  gnb1    = pc802   10.10.1.2   OAI eNB
  uehost1 = pc801   10.10.1.4   OAI UE host

Channel emulation:
  RFsimulator over TCP sockets:
  eNB n: listens on port 40000 + n * 10
  UE n : connects to 10.10.1.2:(40000 + n * 10)
  Bandwidth: 50 PRB (10 MHz, N_RB_DL=50, Band 7, 2680MHz) matching srsRAN.
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
    enb_id = 0xe00 + i
    return f"""# SPDX-License-Identifier: LicenseRef-CSSL-1.0

Active_eNBs = ( "enb_ue{i}");
Asn1_verbosity = "none";

eNBs =
(
 {{
    eNB_ID    =  {enb_id};
    cell_type =  "CELL_MACRO_ENB";
    eNB_name  =  "enb_ue{i}";
    
    tracking_area_code = 7;
    plmn_list = ( {{ mcc = 999; mnc = 70; mnc_length = 2; }} );
    tr_s_preference     = "local_mac";
    rrc_inactivity_threshold = 0;

    component_carriers = (
      {{
      node_function             = "3GPP_eNODEB";
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
      tx_gain                   = 90;
      rx_gain                   = 115;
      pbch_repetition           = "FALSE";
      prach_root                = 0;
      prach_config_index        = 0;
      prach_high_speed          = "DISABLE";
      prach_zero_correlation    = 1;
      prach_freq_offset         = 2;
      pucch_delta_shift         = 1;
      pucch_nRB_CQI             = 0;
      pucch_nCS_AN              = 0;
      pucch_n1_AN               = 0;
      pdsch_referenceSignalPower= -29;
      pdsch_p_b                 = 0;
      pusch_n_SB                = 1;
      pusch_enable64QAM         = "DISABLE";
      pusch_hoppingMode         = "interSubFrame";
      pusch_hoppingOffset       = 0;
      pusch_groupHoppingEnabled = "ENABLE";
      pusch_groupAssignment     = 0;
      pusch_sequenceHoppingEnabled = "DISABLE";
      pusch_nDMRS1              = 1;
      phich_duration            = "NORMAL";
      phich_resource            = "ONESIXTH";
      srs_enable                = "DISABLE";

      pusch_p0_Nominal          = -96;
      pusch_alpha               = "AL1";
      pucch_p0_Nominal          = -96;
      msg3_delta_Preamble       = 6;
      pucch_deltaF_Format1      = "deltaF2";
      pucch_deltaF_Format1b     = "deltaF3";
      pucch_deltaF_Format2      = "deltaF0";
      pucch_deltaF_Format2a     = "deltaF0";
      pucch_deltaF_Format2b     = "deltaF0";
 
      rach_numberOfRA_Preambles                = 64;
      rach_preamblesGroupAConfig               = "DISABLE";
      rach_powerRampingStep                    = 4;
      rach_preambleInitialReceivedTargetPower  = -108;
      rach_preambleTransMax                    = 10;
      rach_raResponseWindowSize                = 10;
      rach_macContentionResolutionTimer        = 48;
      rach_maxHARQ_Msg3Tx                      = 4;

      pcch_default_PagingCycle                 = 128;
      pcch_nB                                  = "oneT";
      bcch_modificationPeriodCoeff             = 2;
      ue_TimersAndConstants_t300               = 1000;
      ue_TimersAndConstants_t301               = 1000;
      ue_TimersAndConstants_t310               = 1000;
      ue_TimersAndConstants_t311               = 10000;
      ue_TimersAndConstants_n310               = 20;
      ue_TimersAndConstants_n311               = 1;
      ue_TransmissionMode                      = 1;
    }}
  );

    srb1_parameters :
    {{
        timer_poll_retransmit    = 80;
        timer_reordering         = 35;
        timer_status_prohibit    = 0;
        poll_pdu                 =  4;
        poll_byte                =  99999;
        max_retx_threshold       =  4;
    }};

    SCTP :
    {{
        SCTP_INSTREAMS  = 2;
        SCTP_OUTSTREAMS = 2;
    }};

    enable_measurement_reports = "no";
    mme_ip_address = ({{ ipv4 = "10.10.1.1"; port = 36412; }});
    enable_x2         = "no";
    t_reloc_prep      = 1000;
    tx2_reloc_overall = 2000;
    t_dc_prep         = 1000;
    t_dc_overall      = 2000;

    NETWORK_INTERFACES : 
    {{
        ENB_IPV4_ADDRESS_FOR_S1_MME              = "{gtp_ip(i)}/24";
        ENB_IPV4_ADDRESS_FOR_S1U                 = "{gtp_ip(i)}/24";
        ENB_PORT_FOR_S1U                         = 2152;
        ENB_IPV4_ADDRESS_FOR_X2C                 = "{gtp_ip(i)}/24";
        ENB_PORT_FOR_X2C                         = 36422;
    }};
  }}
);

MACRLCs =
(
  {{
    num_cc          = 1;
    tr_s_preference = "local_L1";
    tr_n_preference = "local_RRC";
    phy_test_mode   = 0;
    puSch10xSnr     =  160;
    puCch10xSnr     =  160;
  }}
);

L1s =
(
  {{
    num_cc = 1;
    tr_n_preference = "local_mac";
  }}
);

RUs =
(
  {{
    local_rf                      = "yes";
    nb_tx                         = 1;
    nb_rx                         = 1;
    att_tx                        = 3;
    att_rx                        = 6;
    bands                         = [7];
    max_pdschReferenceSignalPower = -27;
    max_rxgain                    = 115;
    eNB_instances                 = [0];
  }}
);

rfsimulator :
  {{
    serveraddr = "server";
    serverport = {rfsim_port};
  }};

THREAD_STRUCT =
(
  {{
    parallel_config    = "PARALLEL_SINGLE_THREAD";
    worker_config      = "WORKER_ENABLE";
  }}
);

log_config : 
  {{
     global_log_level                      ="info"; 
     hw_log_level                          ="info"; 
     phy_log_level                         ="info"; 
     mac_log_level                         ="info"; 
     rlc_log_level                         ="info"; 
     pdcp_log_level                        ="info"; 
     rrc_log_level                         ="info"; 
}};
"""

def make_ue_conf(i):
    imsi_msin = f"{i:010d}"
    imei = f"35349006987{i:04d}"[:15]
    return f"""PLMN: {{
    PLMN0: {{
           FULLNAME="Open5GS";
           SHORTNAME="Open5GS";
           MNC="70";
           MCC="999";
    }};
}};

UE0:
{{
    USER: {{
        IMEI="{imei}";
        MANUFACTURER="OAI";
        MODEL="LTE UE";
        PIN="0000";
    }};

    SIM: {{
        MSIN="{imsi_msin}";
        USIM_API_K="465B5CE8B199B49FAA5F0A2EE238A6BC";
        OPC="E8ED289DEBA952E4283B54E88E6183CA";
        MSISDN="00000001";
    }};

    HPLMN= "99970";
    UCPLMN_LIST = ();
    OPLMN_LIST = ("99970");
    OCPLMN_LIST = ();
    FPLMN_LIST = ();
    EHPLMN_LIST= ();
}};
"""

def install():
    print("Generating OAI 4G LTE configs for 50 UEs & 50 eNBs...")

    with open("/tmp/install_oai_enb_confs.sh", "w") as f:
        f.write("sudo mkdir -p /etc/oai_enb /tmp/gnb_logs_oai\n")
        for i in range(1, 51):
            f.write(f'sudo tee /etc/oai_enb/enb_ue{i}.conf > /dev/null << "EOF"\n{make_enb_conf(i)}EOF\n')
        f.write('echo "Installed 50 OAI eNB configs in /etc/oai_enb"\n')

    with open("/tmp/install_oai_ue_confs.sh", "w") as f:
        f.write("sudo mkdir -p /etc/oai_ue /tmp/ue_logs_oai /tmp/oai_ue_ctx\n")
        for i in range(1, 51):
            f.write(f"sudo mkdir -p /tmp/oai_ue_ctx/ue{i}\n")
            f.write(f'sudo tee /tmp/oai_ue_ctx/ue{i}/ue.conf > /dev/null << "EOF"\n{make_ue_conf(i)}EOF\n')
            f.write(f"/opt/openairinterface5g/cmake_targets/ran_build/build/conf2uedata -c /tmp/oai_ue_ctx/ue{i}/ue.conf -o /tmp/oai_ue_ctx/ue{i} > /dev/null 2>&1\n")
        f.write('echo "Installed 50 OAI UE NVRAMs in /tmp/oai_ue_ctx/ue1..50"\n')

    print("Deploying eNB configs to pc802...")
    with open("/tmp/install_oai_enb_confs.sh", "r") as f:
        out = ssh("saish@pc802.emulab.net", f.read())
        print(f"  {out}")

    print("Deploying UE NVRAMs to pc801...")
    with open("/tmp/install_oai_ue_confs.sh", "r") as f:
        out = ssh("saish@pc801.emulab.net", f.read())
        print(f"  {out}")

    print("Config generation & deployment finished.")

if __name__ == "__main__":
    install()
