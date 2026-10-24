# Working on this repo

## Changing a contract: update every caller in the same change

Before you change a function's signature, return type, the exceptions it
raises, or rename, move or remove anything, find every place that uses it and
update them in the same commit. Search the whole repo, not just `ttsbot/`:

```bash
git grep -n "BotConfigStore(" -- ttsbot scripts tests docs
```

- `scripts/` is easy to miss: nothing imports the scripts and the tests don't
  run them.
- When the *exception type* changes, the callers' `except` clauses count as
  callers too. Moving saves from JSON files to SQLite turned `OSError` into
  `sqlite3.Error`, which every `except OSError` around a save silently stopped
  catching.
- When you fix a pattern at one site, grep for the same pattern elsewhere and
  fix all of them, or say in the commit message which ones you left and why.
  Example: `VoiceCooldown` was handled at the `ensure_voice` call in
  `playback.py` but not at its other callers.
- In the PR description, list the callers you checked for each changed contract.

## Before committing

```bash
ruff check .
basedpyright
python -m unittest discover -s tests -p "test_*.py"
```

`basedpyright` (`pip install -r requirements-dev.txt`) fails only on type errors
that are missing from `.basedpyright/baseline.json`. Don't add new errors to
the baseline to get CI green: fix them. Once you've fixed baselined ones, shrink
the file with `basedpyright --writebaseline`. Typed parameters are what let the
check catch stale callers, so annotate the arguments of anything you change.
