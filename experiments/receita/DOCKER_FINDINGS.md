# Receita Docker investigation — 2026-09-14

## Confirmed result

Receita issues a token in Linux Chrome, but `/api/Emissao/verificar` rejects it
with HTTP 400 and `statusValidacao=CaptchaFalhaValidacao`. Token length alone
does not demonstrate acceptance. No Docker-only fix was validated.

| Experiment | Result |
| --- | --- |
| Chrome 153, verification via fetch inside the same incognito browser session | CAPTCHA rejected |
| Same session without incognito | CAPTCHA rejected |
| pt-BR preferences + America/Sao_Paulo timezone | CAPTCHA rejected |
| Submit Receita's own form with those locale settings | CAPTCHA rejected |
| Enable real SwiftShader software rendering | WebGL became available; CAPTCHA rejected |
| Chrome for Testing 152.0.7977.82, matching host major version | CAPTCHA rejected |
| Chrome 152 + locale + SwiftShader + website form | No verification response within 40 seconds; inconclusive |
| Chrome 153 + locale + SwiftShader + Noto/DejaVu fonts | CAPTCHA rejected |
| Above plus Openbox, D-Bus session, removal of lean disable flags | CAPTCHA rejected |
| Windows Chrome 152, fresh incognito profile, identical in-browser verification | HTTP 200; emission HTTP 200 with PDF |

The Windows control took 12.8 seconds and produced a 77,503-byte, one-page PDF
whose extracted document number matches the input. Docker and host public IPv4
addresses matched. Their UTC clocks agreed within the measurement interval.
This does not establish identical IPv6 routes or all network behavior.

Windows had hardware-backed AMD WebGL; Docker initially had no WebGL and later
used SwiftShader. Both reported `navigator.webdriver=false`. Neither this
flag nor the other fingerprint observations prove the reason for rejection.

## What remains unknown

The upstream response does not expose hCaptcha's verification error details or
any risk score. The former claims that a user-agent mismatch or shared profile
caused the failure were hypotheses, not established causes. Matching the user
agent, isolating the profile, and keeping all requests inside Chrome did not
solve the failure.

Useful next comparisons:

1. Native Linux desktop Chrome with hardware graphics, then the same browser
   under a container display. This requires a suitable Linux/GPU environment.
2. Docker HTTP worker using a Windows-hosted browser session. The Windows
   browser works, but this cross-environment flow has not been validated and
   would introduce an external browser dependency.

Do not add the experimental rendering flags, desktop packages, browser version
pin, or fonts to production as fixes: none produced an accepted Docker token.

## Reproduce

`experiments/receita_session.py` accepts the existing authorized live fixture
through `LIVE_DOCUMENT` and `LIVE_BIRTH` environment variables. It logs only
environment details and status summaries, never token or cookie contents.
One fresh token is requested per invocation; unsuccessful verification exits
nonzero. `--form` tests the site's submission and stops at verification.

From the project root, with those environment variables already set:

```powershell
docker run --rm --name receita-session-test --shm-size=2g `
  -e LIVE_DOCUMENT -e LIVE_BIRTH `
  -v "${PWD}\src:/app/src:ro" `
  -v "${PWD}\experiments\receita_session.py:/app/experiment.py:ro" `
  -v "${PWD}\runs:/app/runs" `
  certidoes-cdp python /app/experiment.py --incognito --graphics
```

Each disposable container has its own profile in `/tmp/receita-profile` and
does not mount a shared profile volume or publish a CDP port. Source mounts
avoid rebuilding the image for every diagnostic change. Runtime package
experiments were performed in disposable containers and did not change the
production image.

The host control is saved at `runs/receita_session_host/certificate.pdf`.
