# CDP experiments

These probes validate the Garimpo-style lean Chrome/CDP boundary before any
production crawler is reorganized. They share one local profile and must run
sequentially; each stops before certificate issuance.

```powershell
python experiments\trf1\validate_cdp.py
python experiments\receita\validate_cdp.py --wait-seconds 20
python experiments\fazenda\probe.py --debug-port 9335
```

The shared browser helper is `experiments/shared/cdp.py`; the profile is created
at `experiments/profile/` and ignored by Git. Pass `--profile` to override it.

The Fazenda probe also uses the root `solved.png` as an OpenCV template. It
reports the best match and treats a confidence of at least `0.80` as an
auto-solved Turnstile state; it does not submit a certificate request.
