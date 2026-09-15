# Windows GitHub Actions diagnostic

The workflow `.github/workflows/receita-diagnostic.yml` runs the same CDP helper in parallel on hosted Windows and Linux VMs and stops after `POST /api/Emissao/verificar` for diagnostic modes. Select `fingerprint`, `token`, `verify`, or `issue` when dispatching it. `issue` is Windows-only and runs the complete Receita flow, including `/api/Emissao`, then uploads the test PDF.

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

## Polling the generated PDF

For the authorized test secrets, start an issue run from the Actions tab or CLI:

```powershell
gh workflow run receita-diagnostic.yml --repo gabrielccac/certidoes --ref main -f mode=issue
```

After obtaining the run ID, poll and copy the single PDF artifact locally:

```powershell
python scripts/poll_receita_run.py --repo gabrielccac/certidoes --run-id RUN_ID --output-dir runs/receita
```

The helper waits for completion, downloads the artifact to a temporary directory, copies the PDF to `runs/receita`, and removes its temporary copy. The remote artifact remains until its configured retention expires or it is explicitly deleted. This is suitable for low-volume asynchronous jobs; it is not a synchronous HTTP request path because runner startup and hCaptcha can take a minute or more.

The issue artifact contains a real certificate for the supplied test identity. Keep the repository and artifact permissions private for production identities, reduce `retention-days`, or delete the artifact after successful download.
