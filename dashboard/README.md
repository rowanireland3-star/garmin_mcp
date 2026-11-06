# Training dashboard

`dashboard.py` pulls your Garmin data using the tokens cached by `garmin-mcp-auth`
(`~/.garminconnect`, or `$GARMINTOKENS`) and writes a self-contained `index.html`.
It never asks for a password.

```bash
cd dashboard
uv run dashboard.py            # or: pip install garminconnect==0.3.2 && python dashboard.py
uv run dashboard.py --open     # rebuild and open in your browser
uv run dashboard.py --probe    # print exactly which metrics your account returns
uv run dashboard.py --demo     # preview with made-up data, no login
```

Re-run any time to refresh. Finished days are cached in `.cache/`; delete it (or pass
`--no-cache`) to re-download everything. Race dates, PBs, the easy-HR ceiling, and
per-run type overrides are set at the top of `dashboard.py`.

`index.html` and `.cache/` hold your personal data and are git-ignored.
Charts load Chart.js from cdnjs, so the page needs an internet connection to draw them.
