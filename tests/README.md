# Tests

```bash
.venv/bin/python -m unittest discover -s tests        # all of them, about 15 seconds
.venv/bin/python -m unittest tests.test_parsing -v    # just one file
```

Standard library only, no network and no real osu! install: the mirror tests run against a
local HTTP server that can stall, dribble or serve rubbish on demand, and the lazer tests build
a small fake library out of `.osu` files. `OBD_APP_DIR` points the app at a throwaway data
folder so nothing touches your real settings, queue or downloads.

| File | Covers |
|---|---|
| `test_parsing.py` | ID/track/link parsing, `.osz` naming (including hostile filenames), name keys, match scoring |
| `test_mirrors.py` | Downloads, stall and trickle detection, failover, mirror ranking and rate-limit budgets, the lazer library scan, the child environment |
| `test_app.py` | Queue bookkeeping, duplicate detection, what survives a restart, the guards that protect a running download |
