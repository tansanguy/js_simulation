import pandas as pd
from pathlib import Path

RUNS_ROOT = Path('/Users/junlee/Desktop/2026-1/js/final/runs')
RESULT_DIR = Path('/Users/junlee/Desktop/2026-1/js/final/result')

all_avg_rows = []

for summary_path in sorted(RUNS_ROOT.glob('*/*/summary.csv')):
    df = pd.read_csv(summary_path)

    str_cols = df.select_dtypes(include='object').columns.tolist()
    num_cols = df.select_dtypes(include='number').columns.tolist()

    # 평균 행 식별 (scenario 컬럼에 _avg 포함)
    is_avg = df['scenario'].str.contains('_avg', na=False)

    # 각 crosswalk 폴더에 저장
    out_path = summary_path.parent / 'final_result.csv'
    df.to_csv(out_path, index=False, encoding='utf-8-sig')

    # 평균 행만 수집
    avg_rows = df[is_avg].copy()
    # 식별을 위해 group, crosswalk_id 복원 (summary_path에서 추출)
    group = summary_path.parts[-3]
    crosswalk_id = summary_path.parts[-2]
    avg_rows['group'] = group
    avg_rows['crosswalk_id'] = crosswalk_id
    all_avg_rows.append(avg_rows)

    print(f'  ✓ {group}/{crosswalk_id}')

# result 폴더에 전체 평균 모음 저장
summary_df = pd.concat(all_avg_rows, ignore_index=True)

# group, crosswalk_id를 앞으로
cols = ['group', 'crosswalk_id'] + [c for c in summary_df.columns if c not in ('group', 'crosswalk_id')]
summary_df = summary_df[cols]

out_summary = RESULT_DIR / 'final_summary.csv'
summary_df.to_csv(out_summary, index=False, encoding='utf-8-sig')
print(f'\n✓ {out_summary}  ({len(summary_df)} rows)')
