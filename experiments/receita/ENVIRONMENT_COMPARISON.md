# Receita: Windows versus Docker browser boundary

Observed on 2026-09-14 with `experiments/receita_compare.py` against the public CPF page. Both probes used a new profile, incognito mode, the same Playwright/CDP code, and the same local machine/network. A second run in each environment executed hCaptcha but made **no** Receita API call and issued **no** certificate. Raw traces are ignored under `runs/receita_{env,token}_{windows,docker}.json`; they contain no CPF, birth date, cookie, or token value.

| Area | Windows | Docker | Relevance |
| --- | --- | --- | --- |
| OS / display | Windows 11, real desktop | Debian Bookworm under WSL2, Xvfb 1920×1080×24 | JavaScript/browser signals differ. |
| Chrome | 152.0.7977.84 | 153.0.8010.36 | UA and Client Hints differ; the Dockerfile installs unpinned `google-chrome-stable`. |
| Launch flags | Shared lean-profile flags plus `--incognito` | Same, plus `--no-sandbox` and `--disable-dev-shm-usage` | Exact arguments are captured in both token traces. |
| Browser language / timezone | `pt-BR`, `America/Sao_Paulo` | `en-US`, `UTC` | Request `Accept-Language` and JavaScript locale/time differ. |
| Graphics | AMD WebGL available | WebGL unavailable | Canvas hash and GPU signal differ. |
| Other JavaScript signals | `Win32`, 32 GiB device memory | `Linux x86_64`, 16 GiB | `navigator.webdriver` was `false` in both; screen size 1920×1080 in both. |
| Python / transport package | Python 3.12.11, Playwright 1.62.0, curl_cffi 0.16.3 | Python 3.12.14, same package versions | Package version is not the observed difference. |
| DNS / proxy | Receita `189.9.176.194`, hCaptcha JS `104.19.229.21`/`104.19.230.21`; no configured proxy | Same | Public egress IP and upstream scoring were not measured. |

The public page returned HTTP 200 from the same Receita IP over HTTP/1.1 in both environments. The hCaptcha JavaScript returned HTTP 200 from the same IP; Chrome used HTTP/3 on Windows and HTTP/2 in Docker in these runs. Response headers for these resources were materially the same (`content-type`, encoding, server, cache policy). Each trace saw 30 relevant requests and no HTTP error responses. The page reached `document.readyState=complete`, found one widget and two hCaptcha frames, and exposed `hcaptcha.execute` in both. `hcaptcha.execute` returned a token in both runs (lengths 2376 and 2199; tokens were not saved). Token length is not a validity or quality score.

The request trace records the actual CDP wire headers, response headers/status/protocol/IP, and initiator type/URL (with query strings removed). For example, the hCaptcha JS request was parser-initiated by the Receita page in both runs. Windows sent `Accept-Language: pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7`, `sec-ch-ua-platform: "Windows"`, and Chrome 152 UA/Client Hints; Docker sent `en-US,en;q=0.9`, `"Linux"`, and Chrome 153 UA/Client Hints. Neither public-page trace proves API validation succeeds.

The production path is `CdpChrome.start()` → page navigation → `hcaptcha.execute` → **close Chrome** → `curl_cffi.requests.Session(impersonate="chrome")` POST `/api/Emissao/verificar` → POST `/api/Emissao` → PDF. The first API POST sends the token and hard-coded `pt-BR`/origin/referer headers, but no browser cookies. It overrides curl_cffi's UA with the actual browser UA, while curl_cffi selects its own Chrome TLS/HTTP fingerprint. Browser Client Hints and other browser-only headers are not copied. The second API POST sends cookies returned by verification, not cookies from the Chrome page. These are real transport/session discontinuities in **both** environments; they are candidates for a Docker-specific rejection only if the upstream evaluates the different Linux/browser signals more strictly. The current code retries every HTTP 400 with a new token and discards the response body when raising `CertificateError`, which hides whether the 400 is about CAPTCHA, document, or another validation rule.

The authorized validation matrix established the failure boundary:

| Environment / transport | `/api/Emissao/verificar` |
| --- | --- |
| Windows Chrome fetch | HTTP 200, `status: Emitida` |
| Windows `curl_cffi` | HTTP 200, `status: Emitida` |
| Docker Chrome fetch | HTTP 400, `statusValidacao: CaptchaFalhaValidacao` |
| Docker `curl_cffi` | HTTP 400, `statusValidacao: CaptchaFalhaValidacao` |

The Docker result stayed HTTP 400 after matching timezone and language, using Chrome 152 Linux, enabling SwiftShader WebGL, removing each Docker-only flag, using a non-incognito profile, adding 228 font families, and overriding UA/Client Hints/platform to the observed Windows values. The real form-button flow failed the same way. The host and Docker egress IP were identical. This rules out a single missing header, curl TLS impersonation, Chrome major version, locale, viewport, font package, WebGL availability, or the direct `execute()` call as a sufficient fix. The remaining difference is the real Windows versus Linux browser/runtime/device execution and hCaptcha's server-side risk decision; hCaptcha documents that it combines session, behavior, network, device, and action signals rather than relying on one spoofable field.

## How to preserve the Windows execution

The recommended architecture is to keep the API, queue, and PDF storage in Docker, but run a small Receita worker on Windows. That worker launches the isolated Windows Chrome profile, performs the complete Receita flow, and returns only a redacted result plus the PDF through an authenticated local service or private queue. This preserves the Windows kernel, GPU/font stack, browser process, and session together.

The repository now includes `.github/workflows/receita-windows-diagnostic.yml`, which tests this idea on a GitHub-hosted `windows-2025` runner using repository secrets and stops after validation. Setup and interpretation are documented in `experiments/receita/GITHUB_ACTIONS.md`.

An alternative is remote Windows Chrome over CDP. Playwright officially supports `connect_over_cdp`, but CDP has lower fidelity than Playwright's native protocol and must stay behind an authenticated tunnel; never expose the debugging port unauthenticated on `0.0.0.0`. A Windows VM can host the same worker for isolation. Windows Sandbox is disposable and useful for one-shot jobs, but is a poor long-lived service boundary.

A Windows Docker container is not a Windows desktop equivalent. Microsoft documents that applications requiring a desktop GUI are not supported in Windows containers. A Linux container cannot inherit the host Windows kernel, GPU, font rasterizer, or Chrome process; copying packages and flags cannot provide that equivalence.

The probe's `--verify --transport browser|curl` mode makes only `/api/Emissao/verificar` calls and records status, status fields, response-key names, and safe headers. Run each transport once per environment with a fresh token and the same authorized identity, then compare:

1. Both Docker transports fail while Windows passes: run Receita in a real Windows browser/worker.
2. If a future Docker image passes, compare the entire image/runtime change; do not infer that one UA or font made Linux equivalent.
3. After validation passes, test `/api/Emissao`, cookie continuity, and PDF handling separately.

`LIVE_DOCUMENT` and `LIVE_BIRTH` must be supplied privately at runtime; the probe never writes their values. The authorized validation used the supplied test identity and stopped before `/api/Emissao`; no certificate was issued.

References: [hCaptcha token/verification lifecycle](https://docs.hcaptcha.com/), [hCaptcha bot-detection signals](https://www.hcaptcha.com/learning/privacy/bot-detection-without-fingerprinting/), [curl_cffi impersonation targets](https://curl-cffi.readthedocs.io/en/latest/impersonate/targets.html), [Playwright Docker guidance and remote connection](https://playwright.dev/python/docs/docker), [Playwright CDP connection](https://playwright.dev/python/docs/api/class-browsertype), [Microsoft Windows-container limitations](https://learn.microsoft.com/en-us/virtualization/windowscontainers/quick-start/lift-shift-to-containers), and [Windows Sandbox CLI](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-cli).
