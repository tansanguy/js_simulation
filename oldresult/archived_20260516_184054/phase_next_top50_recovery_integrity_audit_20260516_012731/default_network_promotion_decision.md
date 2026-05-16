# Default Network Promotion Decision

## Decision
- signal_fix_net_v1 default candidate: NO
- generated_signal_net_v1 default candidate: NO (treat as expansion net due to NEW_SIGNAL_INSTALLATION)

## Evidence
- signal_fix main12 preservation: False
- generated_signal main12 preservation: False
- signal_fix seed1 ready count: 9 (expected 9)
- generated_signal seed1 ready count: 7 (expected 7)
- signal_fix HIGH_RISK non-candidate TLS changes: 0
- generated_signal HIGH_RISK non-candidate TLS changes: 0

## Recommendation
- Keep current net main12 as the primary experiment baseline group.
- Use signal_fix_net_v1 as additive recovery net only if non-candidate TLS risk is zero and main12 remains preserved.
- Keep generated_signal_net_v1 as expansion experiment net, not default main net.

## Why Baseline Must Be Separate By Net
- Signal plan and controlled-link structure differ by net.
- baseline_extension_count==0 must be verified per net-specific tlLogic.
- Mixing baselines across current/signal_fix/generated nets can bias smart-vs-baseline comparisons.

## Pending Before 30 Seed
- Confirm network_integrity_comparison.csv and tls_diff_summary.csv show no unacceptable HIGH_RISK on non-candidate TLS.
- Confirm seed1 validation CSVs stay PASS for all recovered candidates.
- Run per-net baseline independently for current, signal_fix, and generated_signal groups.
