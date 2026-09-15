# Receita CDP experiment

This smoke test launches the installed Chrome with the shared lean profile
using the lean flags from the shared CDP helper, attaches through CDP, loads
Receita's CPF certificate page, checks the hCaptcha widget, and probes token
generation. It never sends a document or certificate request.

```powershell
python experiments/receita/validate_cdp.py
```

The JSON output is the experiment record; `token_probe` contains only token
status and length, never the token itself. Run sequentially with the TRF1 and
Fazenda probes; use `--profile` to override the shared profile path.
