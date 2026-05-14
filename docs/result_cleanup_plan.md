# Result Cleanup Plan

## Keep

- `result/phase6_transition_after_recovery_20260514_220549/phase6_smoke_baseline_vs_smart_compare_BATCH03_5SEC_SORTED_FINAL.csv`
- `result/phase6_transition_after_recovery_20260514_220549/phase6_mini_experiment_candidates_batch03_only.csv`
- `result/phase6_mini_experiment_batch03_5sec_sorted_baseline_20260514_220549/phase6_smoke_baseline_results.csv`
- `result/phase6_mini_experiment_batch03_5sec_sorted_smart_20260514_220549/phase6_smoke_smart_results.csv`
- `result/phase6_mini_experiment_batch03_5sec_sorted_smart_20260514_220549/phase6_smoke_smart_extension_events.csv`
- `result/**/batch_01_run/recovery_tls_batch_network_v1.net.xml`
- `result/**/batch_02_run/recovery_tls_batch_network_v1.net.xml`
- `result/**/batch_03_run/recovery_tls_batch_network_v1.net.xml`

## Compress Candidate

- Large `net.xml`, `osm`, and `xml` artifacts that are useful as reference but not needed in GitHub.
- Human-readable summaries that can be packed into smaller md/csv deliverables.
- Duplicate network outputs that differ only by rerun timestamp.

## Delete Candidate

- Failed smoke run outputs.
- Placeholder-command runs.
- Sorted predecessors from earlier mini results.
- Large debug traces.
- Duplicate `net.xml` files that are not the batch-owned canonical copy.

## Do Not Delete

- Batch01, batch02, and batch03 run folders themselves.
- Any `recovery_tls_batch_network_v1.net.xml` inside those batch run folders.
- The required keep-list CSV files above.

## User Check Commands

```bash
du -sh result
find result -type f -size +50M -print
find result -name "*debug_trace.csv" -print
find result -name "*.net.xml" -size +50M -print
```

## Notes

- `.gitignore` does not remove already tracked files from Git history.
- Tracked large files need explicit stage and later cleanup decisions.
- Cleanup should preserve the batch03 evidence trail first, then reduce noise.

