# GitHub Cleanup and Push Plan

## Target

- Remote repository: `https://github.com/tansanguy/js_simulation`
- Target branch: `simulation-onboarding`

## User Commands

```bash
git remote -v
git branch --show-current
git remote set-url origin https://github.com/tansanguy/js_simulation.git
git fetch origin
git status --short
```

## Rules

- Do not force push.
- Do not push unless the user decides.
- Do not stage by wildcard.
- Check status before any stage.
- Keep local-only files out of the first stage pass unless the user explicitly approves them.

## Notes

- If local branch name differs from `simulation-onboarding`, switch or create the branch manually before any push decision.
- If push ever becomes necessary, review the exact refspec first.
- Do not use `--force` unless the user explicitly accepts the risk.

