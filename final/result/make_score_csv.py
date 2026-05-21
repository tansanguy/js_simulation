import pandas as pd
import numpy as np

df = pd.read_csv('/Users/junlee/Desktop/2026-1/js/final/result/final_summary.csv')

base = df[df['scenario'] == 'baseline_avg'].set_index(['group', 'crosswalk_id'])
smart = df[df['scenario'] == 'smart_avg'].set_index(['group', 'crosswalk_id'])

# 개선율: (baseline - smart) / |baseline|  →  양수 = 개선
# higher_is_better는 반대 부호
def impr(b, s, higher_is_better=False):
    diff = (b - s) if not higher_is_better else (s - b)
    denom = b.abs().replace(0, np.nan)
    rate = diff / denom
    # baseline=0이고 smart>0인 경우(pet_mean 등) 1.0으로 cap
    if higher_is_better:
        rate = rate.where(b != 0, other=(s > 0).astype(float))
    return rate

DELAY_VARS = ['veh_delay_mean', 'local_500m_avg_delay_sec',
              'surrounding_road_delay_sec', 'local_500m_queue_proxy']

SAFETY_VARS_LIB = ['safety_risk_score', 'very_risky_crossing_count', 'accident_risk_estimate']
SAFETY_VARS_HIB = ['pet_mean']  # higher is better

rows = []
for (grp, cw_id) in base.index:
    b = base.loc[(grp, cw_id)]
    s = smart.loc[(grp, cw_id)]

    row = {'group': grp, 'crosswalk_id': cw_id}

    # ── 원시값 ──────────────────────────────────────────
    for v in DELAY_VARS + SAFETY_VARS_LIB + SAFETY_VARS_HIB:
        row[f'{v}_baseline'] = b[v]
        row[f'{v}_smart'] = s[v]

    # ── 개선율 ──────────────────────────────────────────
    delay_rates = []
    for v in DELAY_VARS:
        r = (b[v] - s[v]) / abs(b[v]) if b[v] != 0 else 0.0
        row[f'{v}_impr'] = round(r * 100, 2)
        delay_rates.append(r)

    safety_rates = []
    for v in SAFETY_VARS_LIB:
        r = (b[v] - s[v]) / abs(b[v]) if b[v] != 0 else 0.0
        row[f'{v}_impr'] = round(r * 100, 2)
        safety_rates.append(r)
    for v in SAFETY_VARS_HIB:
        r = (s[v] - b[v]) / abs(b[v]) if b[v] != 0 else (1.0 if s[v] > 0 else 0.0)
        row[f'{v}_impr'] = round(r * 100, 2)
        safety_rates.append(r)

    # ── 스코어 ──────────────────────────────────────────
    score1 = np.nanmean(delay_rates) * 100
    score2 = (0.6 * np.nanmean(safety_rates) + 0.4 * np.nanmean(delay_rates)) * 100

    row['score1_delay_impr'] = round(score1, 4)
    row['score2_tradeoff'] = round(score2, 4)
    rows.append(row)

result = pd.DataFrame(rows)

# 순위 열 추가
result['rank_score1'] = result['score1_delay_impr'].rank(ascending=False).astype(int)
result['rank_score2'] = result['score2_tradeoff'].rank(ascending=False).astype(int)

result = result.sort_values('score2_tradeoff', ascending=False).reset_index(drop=True)

out = '/Users/junlee/Desktop/2026-1/js/final/result/final_score.csv'
result.to_csv(out, index=False, encoding='utf-8-sig')
print(f'✓ {out}  ({len(result)} rows, {len(result.columns)} cols)')
print()
print(result[['group','crosswalk_id','score1_delay_impr','rank_score1','score2_tradeoff','rank_score2']].to_string())
