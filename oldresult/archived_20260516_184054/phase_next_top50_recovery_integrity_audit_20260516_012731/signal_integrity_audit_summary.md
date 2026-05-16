# Signal Integrity Audit Summary

## Seed1 Validation
- signal_fix ready_for_30seed True count: 9 (expected 9)
- generated_signal ready_for_30seed True count: 7 (expected 7)

## Main12 Preservation
- signal_fix_net_v1: DO_NOT_PROMOTE_AS_DEFAULT_NET
- generated_signal_net_v1: DO_NOT_PROMOTE_AS_DEFAULT_NET

## TLS Diff Risk Snapshot
- signal_fix changed TLS: 0 (HIGH_RISK=0)
- generated_signal changed TLS: 7 (HIGH_RISK=0)

## Remaining 22
- candidates requiring further recovery: 22

## Notes
- This audit reads existing net/xml/csv only. No long SUMO execution is performed by this script.
- generated_signal_net_v1 is treated as NEW_SIGNAL_INSTALLATION expansion net and requires separate baseline.
