# TRF1 CDP experiment

Readiness smoke test for the public TRF1 certificate landing page using the
local Garimpo-style `experiments/shared/cdp.py` implementation.

It launches the installed Chrome binary with the shared lean profile, attaches
via CDP, waits for the TRF1 reCAPTCHA iframe, reports its `k`/`v` query values,
and terminates Chrome. It does not enter a document, click **Emitir**, call the
certificate API, or save certificate data.

Run from the project root:

```powershell
python experiments/trf1/validate_cdp.py
```

Override Chrome with `--chrome` or `CHROME_PATH`. A successful run prints
`"status": "ready"`; `"status": "blocked"` is a diagnostic result and exits
with code 1. The experiment uses Python 3, Playwright (Python package), and a
locally installed Chrome. No SeleniumBase or ChromeDriver is required.
Run this probe sequentially with the Receita and Fazenda probes so Chrome can
own the shared profile without a lock conflict. Use `--profile` to override it.
