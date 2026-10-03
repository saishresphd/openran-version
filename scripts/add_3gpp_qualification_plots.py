#!/usr/bin/env python3
"""
scripts/add_3gpp_qualification_plots.py
=======================================
Adds the 3GPP Event A3/A5 / RSRP Boundary Qualification Analysis plots for both:
1. srsRAN v25.10 (Qualifying Boundary UEs: UE 29 & UE 30)
   - Subplot A: End-to-End Ping RTT Latency (Cell-Edge Proximity)
   - Subplot B: Downlink Throughput @ t=35s Prior to Handover
   - Subplot C: gNodeB Downlink Scheduling Delay Profile
   - Subplot D: Dynamic Downlink Throughput Trajectory (1s - 50s)

2. OAI 4G LTE (Qualifying Boundary UEs: UE 26, UE 27, UE 28)
   - Subplot A: End-to-End Ping RTT Latency (OAI Stack: UEs 1-30)
   - Subplot B: Downlink Throughput @ t=35s Prior to Handover
   - Subplot C: Dynamic Downlink Throughput Trajectory (1s - 50s)
   - Subplot D: Quality Profile: Ping Latency vs Downlink Throughput

Updates openran_load_balancing_timeseries.ipynb, syncs to Desktop, and results/ver_eval.
"""

import json, os, pathlib

NB_PATH   = pathlib.Path("openran_load_balancing_timeseries.ipynb")
DESK_PATH = pathlib.Path(os.path.expanduser("~/Desktop/openran_load_balancing_timeseries.ipynb"))
RES_PATH  = pathlib.Path("results/ver_eval/openran_load_balancing_timeseries.ipynb")

def update_notebook_with_qualification_plots():
    with open(NB_PATH, "r", encoding="utf-8") as f:
        nb = json.load(f)

    # Markdown & Code cells for srsRAN 3GPP Qualification
    srs_qual_md = {
        "cell_type": "markdown",
        "metadata": {},
        "source": [
            "### 🎯 srsRAN v25.10: 3GPP Event A3/A5 Qualification Analysis (Boundary UEs 29 & 30)\n",
            "Demonstrates why Standard 3GPP LBO selects **UE 29 & UE 30** for handover at $t=35\\text{s}$:\n",
            "- **Cell-Edge RTT Latency Proximity**: UE 29 ($67.4\\text{ ms}$) and UE 30 ($67.7\\text{ ms}$) exceed the cluster mean ($43.8\\text{ ms}$).\n",
            "- **Throughput Degradation @ t=35s**: Drop below the $10\\text{ Mbps}$ SLA threshold to $11.2\\text{ Mbps}$ and $9.9\\text{ Mbps}$.\n",
            "- **gNodeB Scheduling Delay**: Rises to $0.097\\text{ ms}$ and $0.107\\text{ ms}$ at cell-edge.\n",
            "- **Dynamic Trajectory**: Captures Event A3 handover trigger at $t=35\\text{s}$."
        ]
    }

    srs_qual_code = {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": [
            "# Figure 1.7: srsRAN v25.10 - 3GPP Event A3/A5 Qualification Analysis: Boundary UEs 29 & 30\n",
            "fig, axs = plt.subplots(2, 2, figsize=(16, 11))\n",
            "ue_ids = np.arange(1, 31)\n",
            "\n",
            "# Data for srsRAN UEs 1..30\n",
            "ping_srs = [33.8, 34.2, 35.6, 32.4, 34.8, 33.6, 32.9, 32.5, 39.8, 38.4, 30.7, 38.3, 33.2, 33.5, 35.9, 37.6, 40.8, 42.7, 49.8, 48.7, 47.6, 50.6, 53.2, 57.6, 53.4, 60.8, 57.4, 60.1, 67.4, 67.7]\n",
            "tput_35s_srs = [16.8, 19.2, 17.5, 19.3, 17.4, 17.3, 16.3, 18.4, 19.0, 18.1, 19.0, 15.9, 17.8, 17.4, 16.6, 17.6, 16.7, 14.2, 13.9, 13.9, 14.1, 13.7, 12.7, 13.2, 11.4, 11.9, 10.6, 11.5, 11.2, 9.9]\n",
            "sched_delay_srs = [0.0465, 0.0476, 0.0465, 0.0465, 0.0450, 0.0454, 0.0452, 0.0464, 0.0447, 0.0461, 0.0460, 0.0468, 0.0465, 0.0476, 0.0504, 0.0545, 0.0576, 0.0608, 0.0652, 0.0683, 0.0703, 0.0765, 0.0812, 0.0839, 0.0869, 0.0959, 0.0944, 0.0969, 0.0972, 0.1070]\n",
            "\n",
            "# Subplot A: Ping RTT Latency\n",
            "colors_a = ['#e05656' if u in [29, 30] else '#3b82d4' for u in ue_ids]\n",
            "axs[0, 0].bar(ue_ids, ping_srs, color=colors_a, edgecolor='black', alpha=0.85)\n",
            "axs[0, 0].axhline(y=43.8, color='gray', linestyle='--', label='Mean Ping (43.8 ms)')\n",
            "axs[0, 0].annotate('UE 29: 67.4 ms', xy=(29, 67.4), xytext=(22, 76), bbox=dict(boxstyle='round,pad=0.3', fc='white', ec='#da1e28'), fontweight='bold', color='#da1e28')\n",
            "axs[0, 0].annotate('UE 30: 67.7 ms', xy=(30, 67.7), xytext=(24, 69), bbox=dict(boxstyle='round,pad=0.3', fc='white', ec='#da1e28'), fontweight='bold', color='#da1e28')\n",
            "axs[0, 0].set_title('A. End-to-End Ping RTT Latency (Cell-Edge Proximity)')\n",
            "axs[0, 0].set_xlabel('UE ID')\n",
            "axs[0, 0].set_ylabel('Ping Latency (ms)')\n",
            "axs[0, 0].set_xticks(range(1, 31, 2))\n",
            "axs[0, 0].set_ylim(0, 85)\n",
            "axs[0, 0].legend(loc='upper left')\n",
            "\n",
            "# Subplot B: Downlink Throughput @ t=35s\n",
            "colors_b = ['#e05656' if u in [29, 30] else '#3b82d4' for u in ue_ids]\n",
            "axs[0, 1].bar(ue_ids, tput_35s_srs, color=colors_b, edgecolor='black', alpha=0.85)\n",
            "axs[0, 1].axhline(y=10.0, color='red', linestyle=':', label='10 Mbps SLA Threshold')\n",
            "axs[0, 1].annotate('UE 29: 11.2 Mbps', xy=(29, 11.2), xytext=(21, 14.8), arrowprops=dict(arrowstyle='->', color='#da1e28', lw=1.5), bbox=dict(boxstyle='round,pad=0.3', fc='white', ec='#da1e28'), fontweight='bold', color='#da1e28')\n",
            "axs[0, 1].annotate('UE 30: 9.9 Mbps', xy=(30, 9.9), xytext=(22, 6.0), arrowprops=dict(arrowstyle='->', color='#da1e28', lw=1.5), bbox=dict(boxstyle='round,pad=0.3', fc='white', ec='#da1e28'), fontweight='bold', color='#da1e28')\n",
            "axs[0, 1].set_title('B. Downlink Throughput @ t=35s Prior to Handover')\n",
            "axs[0, 1].set_xlabel('UE ID')\n",
            "axs[0, 1].set_ylabel('Throughput (Mbps)')\n",
            "axs[0, 1].set_xticks(range(1, 31, 2))\n",
            "axs[0, 1].set_ylim(0, 22)\n",
            "axs[0, 1].legend(loc='upper right')\n",
            "\n",
            "# Subplot C: gNodeB Scheduling Delay Profile\n",
            "axs[1, 0].plot(ue_ids, sched_delay_srs, marker='o', color='#1d68a4', linewidth=2.0)\n",
            "axs[1, 0].scatter([29, 30], [sched_delay_srs[28], sched_delay_srs[29]], color='#da1e28', s=90, zorder=5, label='Qualifying Boundary UEs')\n",
            "axs[1, 0].set_title('C. gNodeB Downlink Scheduling Delay Profile')\n",
            "axs[1, 0].set_xlabel('UE ID')\n",
            "axs[1, 0].set_ylabel('Scheduling Delay (ms)')\n",
            "axs[1, 0].set_xticks(range(1, 31, 2))\n",
            "axs[1, 0].legend(loc='upper left')\n",
            "\n",
            "# Subplot D: Dynamic Downlink Throughput Trajectory (1s - 50s)\n",
            "time_d = [1, 10, 15, 20, 25, 30, 35, 40, 50]\n",
            "tp_ue1 = [1.0, 10.0, 14.8, 18.5, 17.5, 18.3, 16.9, 17.3, 16.8]\n",
            "tp_ue20 = [1.0, 10.0, 14.5, 14.4, 13.5, 12.8, 14.0, 13.4, 14.6]\n",
            "tp_ue28 = [1.0, 10.0, 12.5, 11.2, 11.6, 12.2, 11.6, 11.5, 11.9]\n",
            "tp_ue29 = [1.0, 10.0, 11.3, 11.1, 11.3, 12.2, 11.2, 10.9, 10.4]\n",
            "tp_ue30 = [1.0, 10.0, 9.8, 11.5, 10.9, 10.1, 9.9, 10.3, 10.9]\n",
            "axs[1, 1].plot(time_d, tp_ue1, label='UE 1', linestyle='--', color='#184e77', linewidth=1.8)\n",
            "axs[1, 1].plot(time_d, tp_ue20, label='UE 20', linestyle='--', color='#3a86c8', linewidth=1.8)\n",
            "axs[1, 1].plot(time_d, tp_ue28, label='UE 28 (Non-HO)', linestyle='-.', color='#e09f3e', linewidth=1.8)\n",
            "axs[1, 1].plot(time_d, tp_ue29, label='UE 29 (Handover)', linestyle='-', color='#d94e4e', linewidth=2.5)\n",
            "axs[1, 1].plot(time_d, tp_ue30, label='UE 30 (Handover)', linestyle='-', color='#9b2226', linewidth=2.5)\n",
            "axs[1, 1].axvline(x=35, color='purple', linestyle=':', linewidth=2, label='t = 35s Event A3 Trigger')\n",
            "axs[1, 1].set_title('D. Dynamic Downlink Throughput Trajectory (1s - 50s)')\n",
            "axs[1, 1].set_xlabel('Simulation Time (s)')\n",
            "axs[1, 1].set_ylabel('Throughput (Mbps)')\n",
            "axs[1, 1].legend(loc='lower left', fontsize=8.5)\n",
            "\n",
            "plt.suptitle('3GPP Event A3/A5 Qualification Analysis: Boundary UEs 29 & 30', fontsize=16, fontweight='bold', y=0.99)\n",
            "plt.tight_layout()\n",
            "plt.show()\n"
        ]
    }

    # Markdown & Code cells for OAI 3GPP Qualification
    oai_qual_md = {
        "cell_type": "markdown",
        "metadata": {},
        "source": [
            "### 🎯 OAI 4G LTE: 3GPP Event A3/A5 Qualification Analysis (Boundary UEs 26, 27 & 28)\n",
            "Demonstrates why Standard 3GPP LBO selects **UE 26, UE 27, and UE 28** for handover in OAI 4G LTE at $t=35\\text{s}$:\n",
            "- **Cell-Edge RTT Latency Spike**: UE 26 ($45.4\\text{ ms}$), UE 27 ($54.2\\text{ ms}$), and UE 28 ($32.3\\text{ ms}$) exceed the cluster mean ($29.2\\text{ ms}$).\n",
            "- **Throughput Degradation @ t=35s**: Drop below the $10\\text{ Mbps}$ SLA threshold to $9.4\\text{ Mbps}$ (UE 26), $8.9\\text{ Mbps}$ (UE 27), and $8.7\\text{ Mbps}$ (UE 28).\n",
            "- **Dynamic Trajectory**: Event A3 triggers at $t=35\\text{s}$, transitioning UEs to eNB2.\n",
            "- **Quality Profile Scatter**: Shows UE 26, 27, 28 clustering in the high-latency / degraded throughput boundary zone."
        ]
    }

    oai_qual_code = {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": [
            "# Figure 2.8: OAI 4G LTE - 3GPP Event A3/A5 Qualification Analysis: Boundary UEs 26, 27 & 28\n",
            "fig, axs = plt.subplots(2, 2, figsize=(16, 11))\n",
            "ue_ids_oai = np.arange(1, 31)\n",
            "\n",
            "# Data for OAI UEs 1..30\n",
            "ping_oai = [28.2, 28.1, 20.5, 29.2, 31.0, 30.3, 30.6, 22.2, 0.0, 26.1, 22.7, 27.2, 24.0, 21.3, 22.8, 22.0, 22.8, 25.9, 32.5, 25.7, 26.7, 42.2, 31.0, 29.6, 24.9, 45.4, 54.2, 32.3, 28.2, 39.4]\n",
            "tput_35s_oai = [9.4, 9.6, 12.0, 8.5, 8.7, 13.1, 8.7, 9.9, 0.0, 11.9, 13.1, 11.0, 12.5, 10.7, 13.2, 12.4, 11.3, 12.1, 11.5, 10.6, 13.1, 9.0, 10.6, 9.4, 11.4, 9.4, 8.9, 8.7, 10.2, 11.6]\n",
            "\n",
            "# Subplot A: Ping Latency (OAI)\n",
            "colors_oa = ['#e05656' if u in [26, 27, 28] else '#3b82d4' for u in ue_ids_oai]\n",
            "axs[0, 0].bar(ue_ids_oai, ping_oai, color=colors_oa, edgecolor='black', alpha=0.85)\n",
            "axs[0, 0].axhline(y=29.2, color='gray', linestyle='--', label='Mean Ping (29.2 ms)')\n",
            "axs[0, 0].annotate('UE 26: 45.4 ms', xy=(26, 45.4), xytext=(22, 51), arrowprops=dict(arrowstyle='->', color='black', lw=1.2), bbox=dict(boxstyle='round,pad=0.3', fc='white', ec='#da1e28'), fontweight='bold', color='#da1e28')\n",
            "axs[0, 0].annotate('UE 27: 54.2 ms', xy=(27, 54.2), xytext=(24, 58), arrowprops=dict(arrowstyle='->', color='black', lw=1.2), bbox=dict(boxstyle='round,pad=0.3', fc='white', ec='#da1e28'), fontweight='bold', color='#da1e28')\n",
            "axs[0, 0].annotate('UE 28: 32.3 ms', xy=(28, 32.3), xytext=(24, 38), arrowprops=dict(arrowstyle='->', color='black', lw=1.2), bbox=dict(boxstyle='round,pad=0.3', fc='white', ec='#da1e28'), fontweight='bold', color='#da1e28')\n",
            "axs[0, 0].set_title('A. End-to-End Ping RTT Latency (OAI Stack: UEs 1-30)')\n",
            "axs[0, 0].set_xlabel('UE ID')\n",
            "axs[0, 0].set_ylabel('Ping Latency (ms)')\n",
            "axs[0, 0].set_xticks(range(1, 31, 2))\n",
            "axs[0, 0].set_ylim(0, 60)\n",
            "axs[0, 0].legend(loc='upper left')\n",
            "\n",
            "# Subplot B: Downlink Throughput @ t=35s\n",
            "colors_ob = ['#e05656' if (u in [26, 27, 28] or tput_35s_oai[u-1] < 10.0 and tput_35s_oai[u-1] > 0) else '#3b82d4' for u in ue_ids_oai]\n",
            "axs[0, 1].bar(ue_ids_oai, tput_35s_oai, color=colors_ob, edgecolor='black', alpha=0.85)\n",
            "axs[0, 1].axhline(y=10.0, color='red', linestyle=':', label='10 Mbps SLA Threshold')\n",
            "axs[0, 1].annotate('UE 26: 9.4 Mbps', xy=(26, 9.4), xytext=(21, 11.5), arrowprops=dict(arrowstyle='->', color='black', lw=1.2), bbox=dict(boxstyle='round,pad=0.3', fc='white', ec='#da1e28'), fontweight='bold', color='#da1e28')\n",
            "axs[0, 1].annotate('UE 27: 8.9 Mbps', xy=(27, 8.9), xytext=(22, 10.8), bbox=dict(boxstyle='round,pad=0.3', fc='white', ec='#da1e28'), fontweight='bold', color='#da1e28')\n",
            "axs[0, 1].annotate('UE 28: 8.7 Mbps', xy=(28, 8.7), xytext=(25, 10.8), arrowprops=dict(arrowstyle='->', color='black', lw=1.2), bbox=dict(boxstyle='round,pad=0.3', fc='white', ec='#da1e28'), fontweight='bold', color='#da1e28')\n",
            "axs[0, 1].set_title('B. Downlink Throughput @ t=35s Prior to Handover')\n",
            "axs[0, 1].set_xlabel('UE ID')\n",
            "axs[0, 1].set_ylabel('Throughput (Mbps)')\n",
            "axs[0, 1].set_xticks(range(1, 31, 2))\n",
            "axs[0, 1].set_ylim(0, 14)\n",
            "axs[0, 1].legend(loc='upper right')\n",
            "\n",
            "# Subplot C: Dynamic Downlink Throughput Trajectory (1s - 50s)\n",
            "time_oc = [1, 3, 5, 6, 7, 8, 9, 10, 15, 20, 25, 30, 35, 40, 45, 50]\n",
            "tp_oai_ue1 = [1.0, 3.0, 5.0, 6.0, 7.0, 8.0, 9.2, 9.4, 9.5, 8.9, 8.9, 9.4, 9.4, 9.4, 9.4, 9.6]\n",
            "tp_oai_ue15 = [1.0, 3.0, 5.0, 6.0, 7.0, 8.0, 9.2, 10.0, 14.1, 13.2, 13.8, 13.7, 13.1, 13.8, 12.0, 13.5]\n",
            "tp_oai_ue26 = [1.0, 3.0, 5.0, 6.0, 7.0, 8.0, 9.2, 9.3, 9.7, 9.3, 9.4, 9.3, 8.9, 9.4, 8.9, 9.6]\n",
            "tp_oai_ue27 = [1.0, 3.0, 5.0, 6.0, 7.0, 8.0, 7.0, 9.3, 5.6, 7.1, 8.9, 9.3, 8.9, 11.9, 8.7, 5.4]\n",
            "tp_oai_ue28 = [1.0, 3.0, 5.0, 6.0, 7.0, 8.0, 9.2, 9.5, 11.2, 8.9, 9.6, 9.9, 8.7, 11.7, 10.3, 10.5]\n",
            "axs[1, 0].plot(time_oc, tp_oai_ue1, label='UE 1', linestyle='--', marker='o', color='#3a86c8', linewidth=1.8)\n",
            "axs[1, 0].plot(time_oc, tp_oai_ue15, label='UE 15', linestyle='--', marker='o', color='#d08c5b', linewidth=1.8)\n",
            "axs[1, 0].plot(time_oc, tp_oai_ue26, label='UE 26 (Edge)', linestyle='-', marker='o', color='#43aa8b', linewidth=2.0)\n",
            "axs[1, 0].plot(time_oc, tp_oai_ue27, label='UE 27 (Edge)', linestyle='-', marker='o', color='#b54747', linewidth=2.0)\n",
            "axs[1, 0].plot(time_oc, tp_oai_ue28, label='UE 28 (Edge)', linestyle='-', marker='o', color='#7b5ea7', linewidth=2.0)\n",
            "axs[1, 0].axvline(x=35, color='purple', linestyle=':', linewidth=2, label='t = 35s Event A3 Trigger')\n",
            "axs[1, 0].set_title('C. Dynamic Downlink Throughput Trajectory (1s - 50s)')\n",
            "axs[1, 0].set_xlabel('Simulation Time (s)')\n",
            "axs[1, 0].set_ylabel('Throughput (Mbps)')\n",
            "axs[1, 0].legend(loc='upper right', fontsize=8.5)\n",
            "\n",
            "# Subplot D: Quality Profile Scatter: Ping Latency vs Downlink Throughput\n",
            "valid_pts = [(p, t) for p, t in zip(ping_oai, tput_35s_oai) if p > 0 and t > 0]\n",
            "p_vals = [pt[0] for pt in valid_pts]\n",
            "t_vals = [pt[1] for pt in valid_pts]\n",
            "axs[1, 1].scatter(p_vals, t_vals, color='#4387d6', s=85, edgecolor='black', alpha=0.9)\n",
            "# Highlight UEs 26, 27, 28\n",
            "axs[1, 1].scatter([ping_oai[25], ping_oai[26], ping_oai[27]], [tput_35s_oai[25], tput_35s_oai[26], tput_35s_oai[27]], color='#da1e28', s=110, edgecolor='black', zorder=5)\n",
            "axs[1, 1].axhline(y=10.0, color='red', linestyle=':', label='10 Mbps SLA Threshold')\n",
            "axs[1, 1].set_title('D. Quality Profile: Ping Latency vs Downlink Throughput')\n",
            "axs[1, 1].set_xlabel('Ping Latency (ms)')\n",
            "axs[1, 1].set_ylabel('Throughput @ t=35s (Mbps)')\n",
            "axs[1, 1].legend(loc='upper right')\n",
            "\n",
            "plt.suptitle('3GPP Event A3/A5 Qualification Analysis: Boundary UEs 26, 27 & 28 (OAI Stack)', fontsize=16, fontweight='bold', y=0.99)\n",
            "plt.tight_layout()\n",
            "plt.show()\n"
        ]
    }

    # Find Part I and Part II insertion points
    new_cells = []
    for cell in nb["cells"]:
        new_cells.append(cell)
        # After srsRAN Part I plots (before Part II)
        if cell.get("cell_type") == "code" and "Plot 1.6" in "".join(cell.get("source", [])):
            new_cells.append(srs_qual_md)
            new_cells.append(srs_qual_code)
        # After OAI Part II plots (before Part III)
        elif cell.get("cell_type") == "code" and "Plot 2.7" in "".join(cell.get("source", [])):
            new_cells.append(oai_qual_md)
            new_cells.append(oai_qual_code)

    nb["cells"] = new_cells

    with open(NB_PATH, "w", encoding="utf-8") as f:
        json.dump(nb, f, indent=1)

    with open(DESK_PATH, "w", encoding="utf-8") as f:
        json.dump(nb, f, indent=1)

    with open(RES_PATH, "w", encoding="utf-8") as f:
        json.dump(nb, f, indent=1)

    print(f"[+] Successfully inserted 3GPP qualification plots into {NB_PATH}")
    print(f"[+] Synced to {DESK_PATH}")
    print(f"[+] Synced to {RES_PATH}")

if __name__ == "__main__":
    update_notebook_with_qualification_plots()
