# Phase 6 Next Work Plan

## Completed

- Phase 6 batch03 mini experiment succeeded.
- Final compare file exists at `result/phase6_transition_after_recovery_20260514_220549/phase6_smoke_baseline_vs_smart_compare_BATCH03_5SEC_SORTED_FINAL.csv`.
- Key checks confirmed:
  - `LINK_43439`: expected=5, observed=5, smart extension=0
  - `NODE_7240`: expected=6, observed=6, smart extension=1
  - `LINK_52248`: expected=8, observed=8, smart extension=0
  - `NODE_7240` smart run: `extension_sec=5.0`, `ped_near=6`
- Vehicle route remains off.
- Current result is smoke-level execution validity and one-time 5-second extension logic validation only.

## Remaining

- Vehicle route smoke.
- Vehicle mini.
- Batch03 vehicle experiment.
- Full expansion only after vehicle route validity is stable.
- Result folder cleanup planning and later cleanup execution.
- GitHub remote alignment and careful staging discipline.

## Vehicle Implementation Stage

- Build vehicle route smoke first.
- Keep 1 to 3 vehicles only for route-valid smoke.
- Do not treat 1 to 3 vehicles as final experiment scale.
- After route-valid smoke, run vehicle mini.
- Then run batch03 vehicle experiment.
- Then expand to full experiment only if route validity, demand, and seed stability are strong enough.
- Final experiment needs enough vehicle demand and repeated seeds.
- Keep `5` second, `1`-time extension policy fixed for this phase.

## GitHub Cleanup Stage

- Align remote to `https://github.com/tansanguy/js_simulation.git`.
- Work on branch `simulation-onboarding`.
- Check remote, branch, and status before any stage.
- Stage only explicit files.
- Do not force push.
- Do not push until user decides.

## Result Cleanup Stage

- Preserve required CSV evidence files.
- Keep batch_01_run, batch_02_run, and batch_03_run `recovery_tls_batch_network_v1.net.xml` files.
- Do not delete run folders directly.
- Identify failed smoke runs, placeholder-command runs, old sorted predecessors, debug traces, and duplicate net.xml files as cleanup candidates.
- Compress or archive only after the keep list is locked.

## Forbidden

- Simulation execution.
- File deletion.
- `git add .`
- `git add -A`
- `git commit`
- `git push`
- `git reset --hard`
- `git clean -fd`
- Recursive stage of `result/`
- Treating current mini results as policy-effect proof.

