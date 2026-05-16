#!/usr/bin/env bash
set -euo pipefail

cd /Users/junlee/Desktop/2026-1/js

python3 - <<'PY'
import pandas as pd
from pathlib import Path

out_dir = Path(r"/Users/junlee/Desktop/2026-1/js/result/phase_next_remaining22_p1_signal_fix_20260516_010953")
cand = pd.read_csv(out_dir / 'remaining_p1_signal_fix_candidate_table.csv') if (out_dir / 'remaining_p1_signal_fix_candidate_table.csv').exists() else pd.DataFrame(columns=['crosswalk_id'])
smart = out_dir / 'remaining_p1_seed1_smart_smoke' / 'phase6_smoke_summary.csv'
base = out_dir / 'remaining_p1_seed1_baseline_smoke' / 'phase6_smoke_summary.csv'

smart_df = pd.read_csv(smart) if smart.exists() else pd.DataFrame()
base_df = pd.read_csv(base) if base.exists() else pd.DataFrame()

def b(v):
    return str(v).strip().lower() in ('1','true','yes','y')

def i(v,d=0):
    try:
        if pd.isna(v):
            return d
        return int(float(v))
    except Exception:
        return d

rows=[]
for cid in cand['crosswalk_id'].astype(str).tolist():
    s = smart_df[smart_df['crosswalk_id'].astype(str)==cid]
    b0 = base_df[base_df['crosswalk_id'].astype(str)==cid]
    sr = s.iloc[0].to_dict() if not s.empty else {}
    br = b0.iloc[0].to_dict() if not b0.empty else {}

    smart_ok = bool(sr) and b(sr.get('completed')) and i(sr.get('ped_crossing_person_count'))>0 and i(sr.get('extension_count'))>0
    base_ok = bool(br) and b(br.get('completed')) and i(br.get('ped_crossing_person_count'))>0 and i(br.get('extension_count'),-1)==0
    rows.append({
        'crosswalk_id':cid,
        'smart_completed': b(sr.get('completed')) if sr else False,
        'smart_ped_crossing_person_count': i(sr.get('ped_crossing_person_count')) if sr else 0,
        'smart_extension_count': i(sr.get('extension_count')) if sr else 0,
        'smart_smoke_success': smart_ok,
        'baseline_completed': b(br.get('completed')) if br else False,
        'baseline_ped_crossing_person_count': i(br.get('ped_crossing_person_count')) if br else 0,
        'baseline_extension_count': i(br.get('extension_count'),-1) if br else -1,
        'baseline_smoke_success': base_ok,
        'ready_for_30seed': bool(smart_ok and base_ok),
        'reason': 'seed1 smart/baseline passed' if smart_ok and base_ok else 'seed1 not passed',
    })

out = pd.DataFrame(rows)
out.to_csv(out_dir / 'remaining_p1_seed1_smoke_verdict.csv', index=False)
print('verdict_csv=', out_dir / 'remaining_p1_seed1_smoke_verdict.csv')
print('rows=', len(out))
print('ready=', int(out['ready_for_30seed'].sum()) if not out.empty else 0)
PY
