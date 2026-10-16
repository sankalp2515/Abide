# Contributing

## Set up

See "Development" in the [README](README.md). Python 3.11 and Node 22.

## Before you open a pull request

```bash
ruff check .
pytest -q
python -m intentseal_eval.delegate        # attack rate must stay at 0% and normal tasks at 100%
cd apps/console && npx tsc --noEmit && npm test && npm run build
```

## Rules of the code

- **Gemini proposes, code decides.** A model may draft clauses or write a report; a permission exists only if the
  grounding validator finds it in the employee's words. Never let model output grant an action directly.
- **Fail closed.** If something cannot be understood or checked, actions that leave stay off.
- **Proof from execution.** Tests and the evaluation judge outcomes from the sandbox's execution log, never from what
  an agent says.
- **Every security fix gets a regression test** in `tests/test_delegate_security.py`.
- A new design decision gets a numbered entry in [docs/DESIGN_NOTES.md](docs/DESIGN_NOTES.md).
- Configuration comes from the environment (`INTENTSEAL_*`); no secrets in code or images.
