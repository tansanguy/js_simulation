# Top50 Ready33 Status Update

Output root: `/Users/junlee/Desktop/2026-1/js/result/phase_next_top50_ready33_status_update_20260516_011912`

## Counts

- total top50 rows: `50`
- ready rows: `33`
- remaining manual/reject rows: `17`
- existing ready from top50 pool: `28`
- P1 ready from seed1 smoke: `5`

## Ready breakdown

| net_group | status_label | count |
|---|---:|---|
| current_main | MAIN_READY_CURRENT_NET | 12 |
| generated_signal | GENERATED_SIGNAL_READY | 7 |
| p1_signal_fix | P1_SIGNAL_FIX_READY | 5 |
| signal_fix | SIGNAL_FIX_READY | 9 |

## Remaining labels

| status_label | count |
|---|---:|
| P4_MANUAL_REVIEW | 13 |
| P5_REJECT | 4 |

## Notes

- P1 candidates use `remaining_p1_seed1_smoke_verdict.csv` as source of truth.
- P4/P5 candidates keep manual review / reject labels from `remaining22_recovery_diagnosis.csv`.
- Integrated map is not created yet. This package only prepares next-step plan inputs.
