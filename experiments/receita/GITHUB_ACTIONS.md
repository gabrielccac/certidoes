# Windows GitHub Actions diagnostic

The workflow `.github/workflows/receita-diagnostic.yml` runs the same CDP helper in parallel on hosted Windows and Linux VMs and stops after `POST /api/Emissao/verificar`. Select `fingerprint`, `token`, or `verify` when dispatching it. They never call `/api/Emissao` and never write a token, CPF, birth date, cookie, or PDF.

Create two repository secrets:

- `RECEITA_TEST_DOCUMENT`: test CPF digits
- `RECEITA_TEST_BIRTH`: `YYYY-MM-DD`

Run **Receita Windows browser diagnostic** manually from the Actions tab. The artifact contains the redacted browser fingerprint, launch arguments, safe request/response metadata, and the validation result. It does not contain the secret values.

The first successful run (`34914277257`) returned HTTP 200 with `status: Emitida` on the GitHub-hosted Windows runner. It completed in about 1m31s and uploaded `trace.json`. The runner used Chrome 152 on Windows 2025; its locale/timezone and virtual GPU differed from the developer's Windows desktop, so this proves the Windows runtime path, not a byte-for-byte environment clone.

The workflow uses `uv pip` with the official `setup-uv` cache keyed by `pyproject.toml`. This reduces dependency installation time without changing the browser execution. It cannot shorten hCaptcha scoring or hosted-runner startup.

Run the combined workflow with `verify` to measure both OSes in parallel. A successful Linux run would show that the rejection is tied to the particular Docker/runtime/network context; a Linux `CaptchaFalhaValidacao` would keep the Windows runtime as the meaningful difference.

Interpretation:

- Windows runner returns 200 while Docker returns 400: use a Windows worker/VM for the Receita step, or keep this hosted runner as a diagnostic fallback. GitHub-hosted runners are ephemeral and their datacenter IP can change.
- Windows runner also returns `CaptchaFalhaValidacao`: the remaining cause is likely upstream IP/reputation, test identity state, or time-dependent hCaptcha scoring; GitHub Actions is not a production workaround.

GitHub-hosted Windows runners are useful because they provide a real Windows browser/runtime without a dedicated machine, but they are not a stable identity. For production, the durable options are a small Windows VM/worker or an authenticated remote browser service. A Linux container cannot inherit Windows kernel, graphics, font rasterization, or Chrome internals.
