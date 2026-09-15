# Windows GitHub Actions diagnostic

The workflow `.github/workflows/receita-windows-diagnostic.yml` runs the same CDP helper on a GitHub-hosted Windows VM and stops after `POST /api/Emissao/verificar`. It never calls `/api/Emissao` and never writes a token, CPF, birth date, cookie, or PDF.

Create two repository secrets:

- `RECEITA_TEST_DOCUMENT`: test CPF digits
- `RECEITA_TEST_BIRTH`: `YYYY-MM-DD`

Run **Receita Windows browser diagnostic** manually from the Actions tab. The artifact contains the redacted browser fingerprint, launch arguments, safe request/response metadata, and the validation result. It does not contain the secret values.

Interpretation:

- Windows runner returns 200 while Docker returns 400: use a Windows worker/VM for the Receita step, or keep this hosted runner as a diagnostic fallback. GitHub-hosted runners are ephemeral and their datacenter IP can change.
- Windows runner also returns `CaptchaFalhaValidacao`: the remaining cause is likely upstream IP/reputation, test identity state, or time-dependent hCaptcha scoring; GitHub Actions is not a production workaround.

GitHub-hosted Windows runners are useful because they provide a real Windows browser/runtime without a dedicated machine, but they are not a stable identity. For production, the durable options are a small Windows VM/worker or an authenticated remote browser service. A Linux container cannot inherit Windows kernel, graphics, font rasterization, or Chrome internals.
