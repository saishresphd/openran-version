#!/usr/bin/env python3
"""
analyze_9ue_lb_experiment.py
=============================
Analyse results from the 9-UE load-balancing experiment (UE40-49 gNB1→gNB2).

Verifies and extends paper equations (IEEE 10949489):
  Eq.2  P(N)     = α·N^β + γ            (power vs UE count power-law)
  Eq.3  Ptotal   = Pbase + NaU·PaU + NUi·PUi  (component model)
  Eq.4  Psaved   = Pactive − Pswitched         (LB energy saving)
  Eq.5  P(v/p)   = Pbase + Pi·Tu              (power vs throughput per UE)
  Eq.6  P(N,PRB) = β0 + β1·N + β2·PRB_util   (NEW: joint UE+PRB regression)
  Eq.7  P_sched  = γ0 + γ1·proc_us·N         (NEW: scheduling CPU cost)

  New KF: LB threshold  N_lb = (P_plateau − Pbase) / PUi
  New KF: Marginal power saved per migrated UE: ΔP_ue = Pactive(N) − Pactive(N-1)

Inputs (results/ran_9ue_lb/):
  power_gnb1.csv              — RAPL+sys, 1s, gNB1 (phase-tagged)
  power_gnb2.csv              — RAPL+sys, 1s, gNB2 (phase-tagged)
  ran_params_gnb1.csv         — system collector: N, brate, cpu, MCS, SNR
  ran_phy_mac_gnb1.csv        — PHY/MAC: PDSCH/PUSCH/PUCCH aggregates
  lb9ue_summary.txt           — handover latencies (batch run)
  sequential_accumulation.csv — per-UE sequential HO data (same schema as
                                 ue50_60_experiment/master_accumulation.csv)

Also reads existing larger datasets for cross-validation:
  results/ue50_60_experiment/master_accumulation.csv  (UE50-60 LB, 33 rows)
  results/accum_ramp_experiment/dataset_per_ue_count.csv (UE=0-50 power ramp)

Output: results/ran_9ue_lb/analysis_report.txt
        results/ran_9ue_lb/lb9ue_equations.csv  (per-equation coefficient table)
        results/ran_9ue_lb/per_ue_eq4.csv       (per-UE Eq.4 values)
"""

import os, sys, re, csv
import numpy as np
from pathlib import Path
from datetime import datetime, timezone

try:
    from scipy.optimize import curve_fit
    from scipy.stats import pearsonr
    SCIPY = True
except ImportError:
    SCIPY = False
    print("WARNING: scipy not available — curve fits will use numpy polyfit fallback")

# ─── Paths ───────────────────────────────────────────────────────────────────
RESULTS = Path("results/ran_9ue_lb")
OUT_TXT = RESULTS / "analysis_report.txt"
OUT_CSV = RESULTS / "lb9ue_equations.csv"

def load_csv(path, required_cols=None):
    """Load CSV into list of dicts; skip rows with non-numeric required cols."""
    path = Path(path)
    if not path.exists():
        print(f"  MISSING: {path}")
        return []
    rows = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if required_cols:
                try:
                    for c in required_cols:
                        float(row[c])
                except (KeyError, ValueError):
                    continue
            rows.append(row)
    return rows

def fv(row, col, default=0.0):
    try: return float(row[col])
    except: return default

def phase_rows(rows, phase_prefix, col="phase"):
    return [r for r in rows if r.get(col,"").startswith(phase_prefix)]

def mean(v): return sum(v)/len(v) if v else 0.0
def std(v):
    if len(v) < 2: return 0.0
    m = mean(v)
    return (sum((x-m)**2 for x in v)/(len(v)-1))**0.5

# ─── Power-law model ─────────────────────────────────────────────────────────
def power_law(N, alpha, beta, gamma):
    return alpha * N**beta + gamma

def fit_power_law(N_arr, P_arr):
    if not SCIPY or len(N_arr) < 4:
        # fallback: log-linear polyfit after subtracting min
        gamma = min(P_arr) * 0.9
        Y = np.log(np.array(P_arr) - gamma + 1e-9)
        X = np.log(np.array(N_arr) + 1e-9)
        beta, logalpha = np.polyfit(X, Y, 1)
        alpha = np.exp(logalpha)
        return alpha, beta, gamma, 0.0
    try:
        popt, _ = curve_fit(power_law, N_arr, P_arr, p0=[1.0, 0.5, 15.0],
                            bounds=([0,0,0],[100,5,50]), maxfev=5000)
        P_pred = power_law(np.array(N_arr), *popt)
        ss_res = np.sum((np.array(P_arr) - P_pred)**2)
        ss_tot = np.sum((np.array(P_arr) - mean(P_arr))**2)
        r2 = 1 - ss_res/ss_tot if ss_tot > 0 else 0.0
        return popt[0], popt[1], popt[2], r2
    except:
        return 1.0, 1.0, 15.0, 0.0

# ─── Additional output paths ──────────────────────────────────────────────────
OUT_EQ4_CSV = RESULTS / "per_ue_eq4.csv"

# ─── Load data ────────────────────────────────────────────────────────────────
print("Loading data...")
gnb1_pow   = load_csv(RESULTS/"power_gnb1.csv")
gnb2_pow   = load_csv(RESULTS/"power_gnb2.csv")
ran_params = load_csv(RESULTS/"ran_params_gnb1.csv")
phy_mac    = load_csv(RESULTS/"ran_phy_mac_gnb1.csv")

# Sequential per-UE accumulation (primary new dataset from this experiment)
seq_accum  = load_csv(RESULTS/"sequential_accumulation.csv")

# Existing larger datasets for cross-validation
ue50_60_accum = load_csv(Path("results/ue50_60_experiment/master_accumulation.csv"))
accum_ramp    = load_csv(Path("results/accum_ramp_experiment/dataset_per_ue_count.csv"))

print(f"  power_gnb1:          {len(gnb1_pow)} rows")
print(f"  power_gnb2:          {len(gnb2_pow)} rows")
print(f"  ran_params:          {len(ran_params)} rows")
print(f"  ran_phy_mac:         {len(phy_mac)} rows")
print(f"  seq_accumulation:    {len(seq_accum)} rows  (new sequential per-UE LB)")
print(f"  ue50_60_accum:       {len(ue50_60_accum)} rows  (existing UE50-60 LB)")
print(f"  accum_ramp:          {len(accum_ramp)} rows  (UE=0-50 power ramp)")

# Identify columns — power CSVs may use different header names
def detect_power_col(rows):
    if not rows: return None
    for c in ["rapl_pkg0_w","pkg0_w","power_w","rapl_w","pkg_w"]:
        if c in rows[0]: return c
    return None

pow_col_gnb1 = detect_power_col(gnb1_pow) or "rapl_pkg0_w"
pow_col_gnb2 = detect_power_col(gnb2_pow) or "rapl_pkg0_w"

def detect_phase_col(rows):
    if not rows: return "phase"
    for c in ["phase","label","experiment_phase"]:
        if c in rows[0]: return c
    return "phase"

phase_col = detect_phase_col(gnb1_pow)
if not gnb1_pow and ran_params:
    phase_col = detect_phase_col(ran_params)

# ─── Helper: mean power for a phase across a dataset ─────────────────────────
def mean_power(rows, phase_prefix, pow_col):
    subset = [fv(r, pow_col) for r in rows if r.get(phase_col,"").startswith(phase_prefix) and fv(r,pow_col)>0.5]
    return mean(subset), std(subset), len(subset)

def mean_field(rows, phase_prefix, field, pcol=None):
    pc = pcol or phase_col
    subset = [fv(r, field) for r in rows if r.get(pc,"").startswith(phase_prefix) and fv(r,field)>0]
    return mean(subset), std(subset), len(subset)

# ─── Output setup ─────────────────────────────────────────────────────────────
lines = []
def p(s=""): lines.append(s); print(s)

p("=" * 70)
p("  9-UE LOAD-BALANCING EXPERIMENT — EQUATION ANALYSIS")
p(f"  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
p("=" * 70)

# ─── Phase power extracts ─────────────────────────────────────────────────────
# gNB1 phase 1 = baseline 50 UEs
P_gnb1_baseline_mu, P_gnb1_baseline_sd, n1 = mean_power(gnb1_pow, "phase1", pow_col_gnb1)
# gNB1 phase 2 = pre-LB 500 Mbps (Pactive)
P_gnb1_active_mu, P_gnb1_active_sd, n2 = mean_power(gnb1_pow, "phase2", pow_col_gnb1)
# gNB1 phase 5 = post-LB 41 UEs (Pswitched_gnb1)
P_gnb1_switched_mu, P_gnb1_switched_sd, n3 = mean_power(gnb1_pow, "phase5", pow_col_gnb1)
# gNB2 phase 5 = post-LB gNB2 active (Pswitched_gnb2 increment)
P_gnb2_idle_mu, _, _ = mean_power(gnb2_pow, "phase1", pow_col_gnb2)
P_gnb2_active_mu, P_gnb2_active_sd, n4 = mean_power(gnb2_pow, "phase5", pow_col_gnb2)
# gNB1 phase 6 = gNB2 at 500 Mbps
P_gnb2_500m_mu, P_gnb2_500m_sd, n5 = mean_power(gnb2_pow, "phase6", pow_col_gnb2)

p()
p("─── Phase Power Summary ───")
p(f"  [gNB1] Phase 1 baseline (50 UE):       {P_gnb1_baseline_mu:.2f} ± {P_gnb1_baseline_sd:.2f} W  (n={n1})")
p(f"  [gNB1] Phase 2 pre-LB 500Mbps (Pact):  {P_gnb1_active_mu:.2f} ± {P_gnb1_active_sd:.2f} W  (n={n2})")
p(f"  [gNB1] Phase 5 post-LB 41UE (Pswitch): {P_gnb1_switched_mu:.2f} ± {P_gnb1_switched_sd:.2f} W  (n={n3})")
p(f"  [gNB2] Phase 1 idle baseline:           {P_gnb2_idle_mu:.2f} W")
p(f"  [gNB2] Phase 5 post-LB 9UE:             {P_gnb2_active_mu:.2f} ± {P_gnb2_active_sd:.2f} W  (n={n4})")
p(f"  [gNB2] Phase 6 500Mbps 9UE:             {P_gnb2_500m_mu:.2f} ± {P_gnb2_500m_sd:.2f} W  (n={n5})")

# ─── Equation 2: P(N) = α·N^β + γ ────────────────────────────────────────────
p()
p("=" * 70)
p("EQUATION 2: P(N) = α·N^β + γ  (power-law vs UE count)")
p("=" * 70)

# Use ran_params which has ue_count_label + power
if ran_params:
    # Build N→P mapping from ran_params
    ue_pow = {}
    cnt_col = None
    for c in ["ue_count_label","ue_count","nof_ue","ues"]:
        if c in ran_params[0]: cnt_col = c; break
    if cnt_col and pow_col_gnb1 in ran_params[0]:
        for r in ran_params:
            n = fv(r, cnt_col)
            pw = fv(r, pow_col_gnb1)
            if 1 <= n <= 60 and pw > 0.5:
                ue_pow.setdefault(n, []).append(pw)
    # Average per N
    N_vals = sorted(ue_pow.keys())
    P_vals = [mean(ue_pow[n]) for n in N_vals]

    if len(N_vals) >= 4:
        alpha, beta, gamma, r2 = fit_power_law(N_vals, P_vals)
        p(f"  Fitted: α={alpha:.3f} β={beta:.3f} γ={gamma:.3f} W  R²={r2:.3f}")
        p(f"  Data points N={int(min(N_vals))}–{int(max(N_vals))}, n={len(N_vals)} unique UE counts")
        # Plateau detection
        if len(P_vals) > 10:
            P_arr = np.array(P_vals[-10:])
            plateau = mean(P_arr.tolist())
            plateau_sd = std(P_arr.tolist())
            p(f"  Power plateau (top-10 N vals): {plateau:.2f} ± {plateau_sd:.2f} W")
    else:
        p(f"  Insufficient data for Eq.2 fit (n={len(N_vals)} N values)")
        alpha, beta, gamma, r2 = 9.74, 1.06, 15.16, 0.33
        p(f"  Using paper defaults: α={alpha} β={beta} γ={gamma}")
else:
    alpha, beta, gamma, r2 = 9.74, 1.06, 15.16, 0.33
    p("  ran_params not available — using paper defaults")
    p(f"  α={alpha} β={beta} γ={gamma} R²={r2}")

# ─── Equation 3: Ptotal = Pbase + NaU·PaU + NUi·PUi ─────────────────────────
p()
p("=" * 70)
p("EQUATION 3: Ptotal = Pbase + NaU·PaU + NUi·PUi  (component model)")
p("=" * 70)

Pbase = gamma  # idle/base power = γ from Eq.2
# PaU = marginal power per attached UE (no traffic)
# PUi = marginal power per UE with max traffic

# From Eq.2: dP/dN = α·β·N^(β-1)
# At N=1 → PaU ≈ α·β (marginal power at low load)
PaU = alpha * beta
# At N=50 (plateau) → PUi = (Pplateau - Pbase) / 50
if len(ran_params) > 0:
    P_plateau_est = P_gnb1_baseline_mu if P_gnb1_baseline_mu > 0 else (alpha + gamma)
else:
    P_plateau_est = alpha * 50**beta + gamma
PUi = (P_plateau_est - Pbase) / 50 if P_plateau_est > Pbase else 0.299

p(f"  Pbase (γ from Eq.2):   {Pbase:.3f} W")
p(f"  PaU   (α·β at N→1):   {PaU:.3f} W/UE")
p(f"  PUi   ((Pmax-Pbase)/N):{PUi:.3f} W/UE (at N=50)")
p()
# Validate against measured baseline
if P_gnb1_baseline_mu > 0:
    P3_pred = Pbase + 50*PaU + 50*PUi
    err_pct = abs(P3_pred - P_gnb1_baseline_mu)/P_gnb1_baseline_mu*100
    p(f"  Predicted Ptotal(N=50): {P3_pred:.2f} W")
    p(f"  Measured Pbaseline:     {P_gnb1_baseline_mu:.2f} W")
    p(f"  Eq.3 error:             {err_pct:.1f}%")
    verdict3 = "PASS" if err_pct < 15 else "WARN (>15%)"
    p(f"  → {verdict3}")

# ─── Equation 4: Psaved = Pactive − Pswitched ────────────────────────────────
p()
p("=" * 70)
p("EQUATION 4: Psaved = Pactive − Pswitched  (LB energy saving)")
p("=" * 70)

N_lb = 9  # UEs migrated
if P_gnb1_active_mu > 0 and P_gnb1_switched_mu > 0:
    Pactive    = P_gnb1_active_mu       # gNB1 pre-LB (50 UE, 500 Mbps)
    Pswitched  = P_gnb1_switched_mu     # gNB1 post-LB (41 UE)
    Padded_gnb2 = P_gnb2_active_mu - P_gnb2_idle_mu  # extra cost on gNB2

    Psaved_gnb1   = Pactive - Pswitched
    Psaved_system = Psaved_gnb1 - max(0, Padded_gnb2)

    p(f"  N migrated:      {N_lb} UEs")
    p(f"  Pactive (gNB1):  {Pactive:.2f} ± {P_gnb1_active_sd:.2f} W  (pre-LB, 50 UE)")
    p(f"  Pswitched(gNB1): {Pswitched:.2f} ± {P_gnb1_switched_sd:.2f} W  (post-LB, 41 UE)")
    p(f"  Padded (gNB2):   {Padded_gnb2:.2f} W  (gNB2 idle→active delta)")
    p()
    p(f"  Psaved_gNB1:     {Psaved_gnb1:.2f} W  ({Psaved_gnb1/Pactive*100:.1f}%)")
    p(f"  Psaved_system:   {Psaved_system:.2f} W  (after gNB2 overhead)")
    p(f"  Per-UE saving:   {Psaved_gnb1/N_lb:.2f} W/UE migrated")

    verdict4 = "PASS" if Psaved_gnb1 > 0 else "NEGATIVE (system overhead dominates)"
    p(f"  → {verdict4}")

    # Breakeven: how many UEs need to migrate for net system saving?
    if Padded_gnb2 > 0 and PUi > 0:
        N_breakeven = int(Padded_gnb2 / PUi) + 1
        p(f"  Breakeven N_migrate: ≥{N_breakeven} UEs for net system Psaved > 0")
else:
    p("  Insufficient power data for Eq.4")
    Psaved_gnb1 = 0; Psaved_system = 0

# ─── Equation 5: P(v/p) = Pbase + Pi·Tu ─────────────────────────────────────
p()
p("=" * 70)
p("EQUATION 5: P(v/p) = Pbase + Pi·Tu  (power vs throughput per UE)")
p("=" * 70)

# Use ran_params: group by brate → power
if ran_params:
    brate_col = None
    for c in ["dl_brate_mbps","dl_brate","brate_dl"]:
        if c in ran_params[0]: brate_col = c; break

    if brate_col and pow_col_gnb1 in ran_params[0]:
        # Build brate buckets
        brate_pow = {}
        for r in ran_params:
            br = fv(r, brate_col)
            pw = fv(r, pow_col_gnb1)
            if br > 0.1 and pw > 0.5:
                bucket = round(br / 10) * 10  # 10 Mbps buckets
                brate_pow.setdefault(bucket, []).append(pw)
        buckets = sorted(brate_pow.keys())
        if len(buckets) >= 3:
            Tu_vals = [b for b in buckets]
            P5_vals = [mean(brate_pow[b]) for b in buckets]
            # Linear fit: P = Pbase + Pi·Tu
            if SCIPY:
                from scipy.stats import linregress
                slope, intercept, r_val, _, _ = linregress(Tu_vals, P5_vals)
                Pi = slope; Pb5 = intercept; r2_5 = r_val**2
            else:
                coeffs = np.polyfit(Tu_vals, P5_vals, 1)
                Pi, Pb5, r2_5 = coeffs[0], coeffs[1], 0.0
            p(f"  Linear fit: Pbase={Pb5:.2f} W  Pi={Pi:.4f} W/Mbps  R²={r2_5:.3f}")
            p(f"  Brate range: {min(Tu_vals):.0f}–{max(Tu_vals):.0f} Mbps  ({len(buckets)} buckets)")
        else:
            p("  Insufficient brate variation for Eq.5")
            Pi, Pb5, r2_5 = 0.012, Pbase, 0.0
    else:
        p("  brate column not found in ran_params")
        Pi, Pb5, r2_5 = 0.012, Pbase, 0.0
else:
    p("  ran_params not available for Eq.5")
    Pi, Pb5, r2_5 = 0.012, Pbase, 0.0

# ─── Equation 6 (NEW): P(N, PRB) = β0 + β1·N + β2·PRB_util ─────────────────
p()
p("=" * 70)
p("EQUATION 6 (NEW): P = β0 + β1·N + β2·PRB_util  (joint N+PRB model)")
p("=" * 70)

if phy_mac and ran_params:
    # Merge by timestamp proximity: align phy_mac rows (3s) with power rows (1s)
    # Build epoch→power lookup from ran_params
    pow_by_epoch = {}
    epoch_col = "epoch_s" if "epoch_s" in ran_params[0] else None
    if epoch_col:
        for r in ran_params:
            ep = fv(r, epoch_col)
            pw = fv(r, pow_col_gnb1) if pow_col_gnb1 in r else 0.0
            if ep > 0 and pw > 0.5:
                pow_by_epoch[int(ep)] = pw

    # Build regression arrays
    N6, PRB6, P6 = [], [], []
    for r in phy_mac:
        ep = int(fv(r, "epoch_s"))
        prb = fv(r, "prb_util_dl_pct")
        n_ue = fv(r, "active_ue_slots")
        # Find nearest power measurement (±5s window)
        pw = 0.0
        for delta in range(-5, 6):
            if ep+delta in pow_by_epoch:
                pw = pow_by_epoch[ep+delta]
                break
        if pw > 0.5 and n_ue > 0 and prb > 0:
            N6.append(n_ue)
            PRB6.append(prb)
            P6.append(pw)

    if len(N6) >= 10:
        # Multiple linear regression: P = β0 + β1·N + β2·PRB
        X = np.column_stack([np.ones(len(N6)), N6, PRB6])
        Y = np.array(P6)
        try:
            beta_vec, _, _, _ = np.linalg.lstsq(X, Y, rcond=None)
            beta0, beta1, beta2 = beta_vec
            Y_pred = X @ beta_vec
            ss_res = np.sum((Y - Y_pred)**2)
            ss_tot = np.sum((Y - Y.mean())**2)
            r2_6 = 1 - ss_res/ss_tot
            p(f"  Eq.6 coefficients:")
            p(f"    β0 (Pbase):       {beta0:.3f} W")
            p(f"    β1 (per UE):      {beta1:.4f} W/UE")
            p(f"    β2 (per %PRB):    {beta2:.4f} W/%")
            p(f"    R² = {r2_6:.3f}  (n={len(N6)} observations)")
            p()
            p(f"  Interpretation:")
            p(f"    At N=50, PRB=80%: P = {beta0 + 50*beta1 + 80*beta2:.2f} W")
            p(f"    At N=41, PRB=65%: P = {beta0 + 41*beta1 + 65*beta2:.2f} W  (post-LB)")
            p(f"    ΔP per migrated UE (9 UE): {9*beta1:.3f} W")
        except Exception as e:
            p(f"  Eq.6 regression failed: {e}")
            beta0, beta1, beta2, r2_6 = Pbase, PUi, 0.0, 0.0
    else:
        p(f"  Insufficient joint data for Eq.6 (n={len(N6)} rows with N+PRB+P)")
        p("  NOTE: PHY/MAC collector may not have been running during experiment")
        beta0, beta1, beta2, r2_6 = Pbase, PUi, 0.0, 0.0
else:
    p("  ran_phy_mac_gnb1.csv not available for Eq.6")
    p("  Run experiment with PHY/MAC collector active")
    beta0, beta1, beta2, r2_6 = Pbase, PUi, 0.0, 0.0

# ─── Equation 7 (NEW): P_sched = γ0 + γ1·proc_us·N ─────────────────────────
p()
p("=" * 70)
p("EQUATION 7 (NEW): P_sched = γ0 + γ1·(proc_us × N)  (scheduling CPU cost)")
p("=" * 70)

if phy_mac and len(P6) >= 10:
    sched_x, sched_p = [], []
    for idx, r in enumerate(phy_mac):
        proc = fv(r, "pusch_proc_us_mean")
        n_ue = fv(r, "active_ue_slots")
        ep   = int(fv(r, "epoch_s"))
        if proc > 0 and n_ue > 0:
            pw = 0.0
            for delta in range(-5, 6):
                if ep+delta in pow_by_epoch:
                    pw = pow_by_epoch[ep+delta]
                    break
            if pw > 0.5:
                sched_x.append(proc * n_ue)
                sched_p.append(pw)

    if len(sched_x) >= 10:
        if SCIPY:
            from scipy.stats import linregress
            slope7, intercept7, r7, _, _ = linregress(sched_x, sched_p)
            r2_7 = r7**2
        else:
            c7 = np.polyfit(sched_x, sched_p, 1)
            slope7, intercept7, r2_7 = c7[0], c7[1], 0.0
        p(f"  Eq.7 coefficients:")
        p(f"    γ0 (base):           {intercept7:.3f} W")
        p(f"    γ1 (W per proc_us·N): {slope7:.6f}")
        p(f"    R² = {r2_7:.3f}  (n={len(sched_x)} observations)")
        p()
        p(f"  Interpretation:")
        p(f"    At proc_us=100μs, N=50: P = {intercept7 + slope7*100*50:.2f} W")
        p(f"    At proc_us=100μs, N=41: P = {intercept7 + slope7*100*41:.2f} W  (post-LB)")
        p(f"    Scheduling saves per 9-UE LB: {slope7*100*9:.3f} W")
    else:
        p(f"  Insufficient data for Eq.7 (n={len(sched_x)})")
        slope7, intercept7, r2_7 = 0.0, Pbase, 0.0
else:
    p("  ran_phy_mac_gnb1.csv not available for Eq.7")
    slope7, intercept7, r2_7 = 0.0, Pbase, 0.0

# ─── New Key Finding: LB Threshold ───────────────────────────────────────────
p()
p("=" * 70)
p("NEW KEY FINDING: LB Threshold N_lb = (P_plateau − Pbase) / PUi")
p("=" * 70)

if PUi > 0:
    P_plateau = P_gnb1_baseline_mu if P_gnb1_baseline_mu > 0 else (alpha * 50**beta + gamma)
    N_lb_threshold = int((P_plateau - Pbase) / PUi)
    p(f"  Pbase:              {Pbase:.2f} W")
    p(f"  P_plateau (N=50):   {P_plateau:.2f} W")
    p(f"  PUi:                {PUi:.3f} W/UE")
    p(f"  N_lb_threshold:     {N_lb_threshold} UEs")
    p(f"  Interpretation: gNB1 reaches power plateau at N ≈ {N_lb_threshold} UEs.")
    p(f"  Migrating beyond this threshold yields diminishing power return.")
    p()
    # Marginal saving per UE migrated from N=50
    delta_P_per_ue = Psaved_gnb1 / N_lb if (N_lb > 0 and Psaved_gnb1 > 0) else PUi
    p(f"  Measured ΔP per migrated UE (from N=50): {delta_P_per_ue:.3f} W/UE")
    p(f"  Theoretical PUi (Eq.3):                   {PUi:.3f} W/UE")

# ─── Per-UE Sequential Eq.4 Analysis (from sequential_accumulation.csv) ──────
p()
p("=" * 70)
p("SEQUENTIAL PER-UE Eq.4 ANALYSIS  (new dataset: UE49 → UE40 one at a time)")
p("=" * 70)

def per_ue_eq4_from_accum(rows, dataset_label):
    """
    Compute per-UE Eq.4 values from an accumulation CSV.
    For each UE, finds:
      - attach_gnb1 row  → Pactive   (gNB1 power with UE still attached)
      - post_lb_gnb1 row → Pswitched (gNB1 power after UE removed)
      - attach_gnb2 row  → ho_total_ms, attach_ok, RAN params on gNB2
    Returns list of per-UE result dicts.
    """
    if not rows:
        return []
    results_list = []
    # Group by ue_id
    by_ue = {}
    for r in rows:
        uid = r.get("ue_id", "")
        by_ue.setdefault(uid, []).append(r)

    for uid, ue_rows in sorted(by_ue.items(), key=lambda x: int(x[0]) if str(x[0]).isdigit() else 0):
        ev = {r.get("event_type", ""): r for r in ue_rows}
        pre  = ev.get("attach_gnb1",  ev.get("pre_lb_gnb1", {}))
        post = ev.get("post_lb_gnb1", ev.get("post_lb",     {}))
        gnb2 = ev.get("attach_gnb2",  {})

        if not pre or not post:
            continue

        pactive   = fv(pre,  "gnb_total_watts")
        pswitched = fv(post, "gnb_total_watts")
        psaved    = pactive - pswitched
        pgain_gnb2 = fv(gnb2, "gnb_total_watts")
        ho_ms     = fv(gnb2, "ho_total_ms") or fv(post, "ho_total_ms")
        n_gnb1_pre  = fv(pre, "total_ues_gnb1")
        n_gnb1_post = fv(post, "total_ues_gnb1")
        attach_ok = int(fv(gnb2, "attach_ok"))

        # RAN params at time of handover
        pdsch_prb   = fv(pre, "ran_combined_pdsch_prb_mean")
        pusch_snr   = fv(pre, "ran_combined_pusch_snr_mean")
        proc_us     = fv(pre, "ran_combined_pusch_proc_us_mean")
        cpu_pct     = fv(pre, "sysmon_cpu_user_pct")
        iperf_dl    = fv(pre, "iperf_dl_mbps")

        results_list.append({
            "ue_id": uid, "dataset": dataset_label,
            "pactive_w": pactive, "pswitched_w": pswitched,
            "psaved_w": psaved, "pgain_gnb2_w": pgain_gnb2,
            "net_psaved_w": psaved - max(0.0, pgain_gnb2 - 15.0),  # 15W ≈ gNB2 idle
            "ho_latency_ms": ho_ms, "attach_ok": attach_ok,
            "n_gnb1_pre": n_gnb1_pre, "n_gnb1_post": n_gnb1_post,
            "pdsch_prb": pdsch_prb, "pusch_snr": pusch_snr,
            "proc_us": proc_us, "cpu_pct": cpu_pct, "iperf_dl_mbps": iperf_dl,
        })
    return results_list

# Analyse both new (sequential UE40-49) and existing (UE50-60) datasets
seq_eq4 = per_ue_eq4_from_accum(seq_accum, "seq_ue40_49")
old_eq4 = per_ue_eq4_from_accum(ue50_60_accum, "ue50_60")
all_eq4 = seq_eq4 + old_eq4

# Print per-UE table header
hdr = (f"  {'UE':>4}  {'Src':>10}  {'N_pre':>6}  {'Pact_W':>8}  "
       f"{'Psw_W':>8}  {'Psaved_W':>9}  {'HO_ms':>8}  "
       f"{'OK':>3}  {'PDSCH_PRB':>9}  {'proc_us':>8}")
p(hdr)
p("  " + "─" * (len(hdr) - 2))

all_psaved = []; all_ho = []; all_prb = []; all_proc = []

for r in sorted(all_eq4, key=lambda x: (x["dataset"], int(str(x["ue_id"])) if str(x["ue_id"]).isdigit() else 0)):
    ps_str = f"{r['psaved_w']:+.3f}" if r["pactive_w"] > 0 else "  N/A  "
    ho_str = f"{r['ho_latency_ms']:.0f}" if r["ho_latency_ms"] > 0 else "  N/A"
    p(f"  UE{r['ue_id']:>2}  {r['dataset']:>10}  {r['n_gnb1_pre']:>6.0f}  "
      f"{r['pactive_w']:>8.2f}  {r['pswitched_w']:>8.2f}  "
      f"{ps_str:>9}  {ho_str:>8}  "
      f"{'Y' if r['attach_ok'] else 'N':>3}  "
      f"{r['pdsch_prb']:>9.2f}  {r['proc_us']:>8.1f}")
    if r["pactive_w"] > 0 and r["pswitched_w"] > 0:
        all_psaved.append(r["psaved_w"])
    if r["ho_latency_ms"] > 0:
        all_ho.append(r["ho_latency_ms"])
    if r["pdsch_prb"] > 0:
        all_prb.append(r["pdsch_prb"])
    if r["proc_us"] > 0:
        all_proc.append(r["proc_us"])

p()
if all_psaved:
    p(f"  Eq.4 Psaved/UE across all rows (n={len(all_psaved)}):")
    p(f"    mean = {mean(all_psaved):+.3f} W   std = {std(all_psaved):.3f} W")
    p(f"    min  = {min(all_psaved):+.3f} W   max = {max(all_psaved):+.3f} W")
    # Split by dataset
    seq_ps = [r["psaved_w"] for r in seq_eq4 if r["pactive_w"] > 0]
    old_ps = [r["psaved_w"] for r in old_eq4 if r["pactive_w"] > 0]
    if seq_ps:
        p(f"    New (UE40-49):   mean={mean(seq_ps):+.3f} W  n={len(seq_ps)}")
    if old_ps:
        p(f"    Old (UE50-60):   mean={mean(old_ps):+.3f} W  n={len(old_ps)}")

if all_ho:
    p()
    p(f"  Handover Latency (ms) across all UEs (n={len(all_ho)}):")
    p(f"    mean = {mean(all_ho):.0f} ms   std = {std(all_ho):.0f} ms")
    p(f"    min  = {min(all_ho):.0f} ms   max = {max(all_ho):.0f} ms")
    seq_ho = [r["ho_latency_ms"] for r in seq_eq4 if r["ho_latency_ms"] > 0]
    old_ho = [r["ho_latency_ms"] for r in old_eq4 if r["ho_latency_ms"] > 0]
    if seq_ho:
        p(f"    New (UE40-49):   mean={mean(seq_ho):.0f} ms  n={len(seq_ho)}")
    if old_ho:
        p(f"    Old (UE50-60):   mean={mean(old_ho):.0f} ms  n={len(old_ho)}")

if all_prb:
    p()
    p(f"  RAN: PDSCH PRB utilisation (n={len(all_prb)}):  mean={mean(all_prb):.1f}  std={std(all_prb):.1f}")
if all_proc:
    p(f"  RAN: PUSCH proc_us         (n={len(all_proc)}):  mean={mean(all_proc):.1f}  std={std(all_proc):.1f}")

# Write per-UE Eq.4 CSV
if all_eq4:
    eq4_cols = ["ue_id","dataset","n_gnb1_pre","n_gnb1_post","pactive_w",
                "pswitched_w","psaved_w","pgain_gnb2_w","net_psaved_w",
                "ho_latency_ms","attach_ok","pdsch_prb","pusch_snr",
                "proc_us","cpu_pct","iperf_dl_mbps"]
    with open(OUT_EQ4_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=eq4_cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_eq4)
    p(f"\n  Per-UE Eq.4 CSV written: {OUT_EQ4_CSV}")
else:
    p("  No accumulation data available — run the sequential experiment first.")
    p(f"  Expected: {RESULTS/'sequential_accumulation.csv'}")

# ─── Handover Latency Summary ─────────────────────────────────────────────────
p()
p("=" * 70)
p("HANDOVER LATENCIES (from lb9ue_summary.txt / sequential_lb_summary.txt)")
p("=" * 70)

# Prefer sequential summary if it exists
summary_f = RESULTS / "sequential_lb_summary.txt"
if not summary_f.exists():
    summary_f = RESULTS / "lb9ue_summary.txt"
if summary_f.exists():
    txt = summary_f.read_text()
    for line in txt.split("\n"):
        if any(k in line for k in ["UE4", "Avg HO", "Successful", "HO latency",
                                    "mean", "min", "max", "handover"]):
            p(f"  {line.strip()}")
else:
    p("  No summary file found — per-UE table above contains all HO data")

# ─── Equation Summary Table ───────────────────────────────────────────────────
p()
p("=" * 70)
p("EQUATION SUMMARY TABLE")
p("=" * 70)
p(f"  {'Eq':5s} {'Description':40s} {'Value':25s} {'Status':8s}")
p(f"  {'─'*5} {'─'*40} {'─'*25} {'─'*8}")
p(f"  Eq.2   P(N)=α·N^β+γ                     α={alpha:.2f} β={beta:.2f} γ={gamma:.2f}W  R²={r2:.2f}")
p(f"  Eq.3   Ptotal=Pbase+NaU·PaU+NUi·PUi    PUi={PUi:.3f}W/UE, PaU={PaU:.3f}W/UE")
if P_gnb1_active_mu > 0:
    p(f"  Eq.4   Psaved=Pact-Psw                  {Psaved_gnb1:.2f}W gNB1, {Psaved_system:.2f}W system")
p(f"  Eq.5   P(v/p)=Pbase+Pi·Tu               Pi={Pi:.4f}W/Mbps Pbase={Pb5:.2f}W R²={r2_5:.2f}")
p(f"  Eq.6   P=β0+β1·N+β2·PRB                 β1={beta1:.4f}W/UE β2={beta2:.4f}W/% R²={r2_6:.2f}")
p(f"  Eq.7   P_sched=γ0+γ1·(proc_us·N)        γ1={slope7:.6f} R²={r2_7:.2f}")
if PUi > 0:
    p(f"  KF-LB  N_lb_threshold                   {N_lb_threshold} UEs")
p()

# ─── Save report + CSV ────────────────────────────────────────────────────────
RESULTS.mkdir(parents=True, exist_ok=True)
OUT_TXT.write_text("\n".join(lines))
print(f"\nReport written: {OUT_TXT}")

# Summary CSV
eq_rows = [
    {"equation":"Eq2","param":"alpha","value":alpha,"unit":"W","note":"power-law coefficient"},
    {"equation":"Eq2","param":"beta", "value":beta, "unit":"","note":"power-law exponent"},
    {"equation":"Eq2","param":"gamma","value":gamma,"unit":"W","note":"base power"},
    {"equation":"Eq2","param":"R2",   "value":r2,   "unit":"","note":"goodness of fit"},
    {"equation":"Eq3","param":"Pbase","value":Pbase,"unit":"W","note":"idle base power"},
    {"equation":"Eq3","param":"PaU",  "value":PaU,  "unit":"W/UE","note":"attached UE marginal"},
    {"equation":"Eq3","param":"PUi",  "value":PUi,  "unit":"W/UE","note":"active UE marginal"},
    {"equation":"Eq4","param":"Pactive","value":P_gnb1_active_mu,"unit":"W","note":"gNB1 pre-LB"},
    {"equation":"Eq4","param":"Pswitched","value":P_gnb1_switched_mu,"unit":"W","note":"gNB1 post-LB"},
    {"equation":"Eq4","param":"Psaved_gnb1","value":Psaved_gnb1,"unit":"W","note":"gNB1 savings"},
    {"equation":"Eq4","param":"Psaved_system","value":Psaved_system,"unit":"W","note":"system savings"},
    {"equation":"Eq4","param":"Psaved_mean_per_ue","value":mean(all_psaved) if all_psaved else 0,"unit":"W/UE","note":"per-UE mean across all datasets"},
    {"equation":"Eq4","param":"HO_latency_mean_ms","value":mean(all_ho) if all_ho else 0,"unit":"ms","note":"mean HO latency across all UEs"},
    {"equation":"Eq5","param":"Pi","value":Pi,"unit":"W/Mbps","note":"power/throughput slope"},
    {"equation":"Eq5","param":"Pbase5","value":Pb5,"unit":"W","note":"intercept"},
    {"equation":"Eq5","param":"R2","value":r2_5,"unit":"","note":""},
    {"equation":"Eq6","param":"beta0","value":beta0,"unit":"W","note":"NEW joint model"},
    {"equation":"Eq6","param":"beta1","value":beta1,"unit":"W/UE","note":"UE marginal"},
    {"equation":"Eq6","param":"beta2","value":beta2,"unit":"W/%PRB","note":"PRB marginal"},
    {"equation":"Eq6","param":"R2","value":r2_6,"unit":"","note":""},
    {"equation":"Eq7","param":"gamma0","value":intercept7,"unit":"W","note":"NEW sched model"},
    {"equation":"Eq7","param":"gamma1","value":slope7,"unit":"W/(us*UE)","note":""},
    {"equation":"Eq7","param":"R2","value":r2_7,"unit":"","note":""},
]
with open(OUT_CSV,"w",newline="") as f:
    writer = csv.DictWriter(f, fieldnames=["equation","param","value","unit","note"])
    writer.writeheader()
    writer.writerows(eq_rows)
print(f"CSV written: {OUT_CSV}")
