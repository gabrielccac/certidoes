# Certidoes

Certificate generation for the Receita Federal, TJDFT, TRF1, Fazenda DF,
TRT10 and TST portals. The ingest API accepts a job, runs the configured
sources, stores PDFs under `runs/`, and publishes a redacted result summary.

## Requirements

- Python 3.12 and [uv](https://docs.astral.sh/uv/), for local development.
- Docker Desktop or Docker Engine, for the complete ingest image.
- A public HTTPS hostname when Airtable must fetch generated attachments.
- A GitHub fine-grained personal access token for the Receita executor.
- An Airtable personal access token only when using the Airtable provider.

The image contains Google Chrome, Xvfb, Playwright, OpenCV, ddddocr,
`solved.png`, and the persistent CDP profile volume required by the portal
flows. Playwright is used by the browser-backed sources; it is not a separate
service.

## Local setup

```powershell
uv sync
$env:PYTHONPATH = "src"
uv run python -m pytest tests -q
```

Run the API with the inline provider (no Airtable access):

```powershell
$env:INGEST_SECRET = "local-secret"
$env:PUBLIC_BASE_URL = "http://127.0.0.1:8090"
uv run python -m certidoes.ingest.worker `
  --config config/ingest/local.toml --serve --port 8090
```

Submit the test payload:

```powershell
$body = @{
  provider = "local"
  ref = @{
    label = "docker-validation"
    status = "PENDENTE"
    fields = @{
      documento = "12433377617"
      data_nascimento = "2002-05-23"
      primeiro_nome = "Gabriel"
      nome_mae = "Luciana Farago Ribeiro"
    }
  }
} | ConvertTo-Json -Depth 6

Invoke-RestMethod http://127.0.0.1:8090/jobs -Method Post `
  -Headers @{"X-Ingest-Secret" = "local-secret"} `
  -ContentType "application/json" -Body $body
```

The local provider writes a redacted report and does not update Airtable. A
local Receita run still needs the Windows/GitHub executor for hCaptcha; use
the Docker command below for the production-equivalent path.

## Docker deployment

Copy `.env.example` to `.env` and fill every required value. The file is read
when the container starts and is excluded from the image build context.

```powershell
Copy-Item .env.example .env
docker compose --env-file .env -f docker/ingest/compose.yml up --build -d
docker compose --env-file .env -f docker/ingest/compose.yml logs -f api
```

The API is served through Caddy at `INGEST_DOMAIN`. DNS for that hostname must
point to the Docker host so Caddy can issue the HTTPS certificate. The API
expects `POST /jobs` with `X-Ingest-Secret`; `GET /healthz` is the health check.
The profile and generated files live in named Docker volumes. Do not mount the
same Chrome profile concurrently from a local browser and the container.

For a real Airtable record, send only its record ID:

```json
{
  "provider": "airtable",
  "ref": {"record_id": "recXXXXXXXXXXXXXX"}
}
```

The mapping in `config/ingest/pessoas.toml` reads fields by stable Airtable
field ID, claims `PENDENTE` or `TENTAR NOVAMENTE` as `PROCESSANDO`, and writes
`FINALIZADO` or `FALHA` plus the summary and attachment URLs. Run the read-only
mapping check before processing a new base or changed table:

```powershell
uv run python -m certidoes.ingest.worker `
  --config config/ingest/pessoas.toml --check --record recXXXXXXXXXXXXXX
```

## `.env` values

| Variable | Where it comes from |
| --- | --- |
| `AIRTABLE_PAT` | Airtable Developer Hub → Personal access tokens. Grant `data.records:read` and `data.records:write`; add `schema.bases:read` for exact `--check` results. |
| `AIRTABLE_BASE_ID` | The `app...` identifier in the Airtable base URL. |
| `INGEST_SECRET` | A random API secret, for example `python -c "import secrets; print(secrets.token_urlsafe(32))"`. |
| `PUBLIC_BASE_URL` | The public HTTPS URL that Airtable can fetch for `/files/...`; it must not be localhost. |
| `INGEST_DOMAIN` | The DNS hostname used by Caddy, normally the same host as `PUBLIC_BASE_URL`. |
| `RECEITA_EXECUTOR` | `github` for Docker/production. `local` uses the local browser path and is mainly diagnostic. |
| `RECEITA_GITHUB_REPO` | `owner/repository`, normally `gabrielccac/certidoes`. |
| `RECEITA_GITHUB_TOKEN` | A fine-grained GitHub PAT restricted to this repository with `Actions: Read and write`. It is injected at runtime and never belongs in the Dockerfile. |
| `RECEITA_GITHUB_REF` | Git ref containing the workflow; use `main` in production. |

Create the GitHub token at
<https://github.com/settings/personal-access-tokens/new>. Select only this
repository under **Repository access**, then set **Repository permissions →
Actions → Read and write**. The workflow's encrypted payload private key is a
GitHub repository secret named `RECEITA_PAYLOAD_PRIVATE_KEY`; it must not be
copied into `.env`. The matching public key is bundled in
`src/certidoes/receita_github_public.pem`.

The Docker process dispatches the Windows-only Receita workflow with an
encrypted CPF/date payload. The workflow uploads a short-lived PDF artifact;
the API downloads it, validates the `%PDF-` signature, saves it locally, and
deletes the artifact. It is transport, not permanent storage.

## Project structure

```text
src/certidoes/sources/       One adapter per certificate portal
src/certidoes/ingest/        API, providers, mapping, artifacts and worker
src/certidoes/registry.py    Source registry used by TOML mappings
config/ingest/local.toml     Inline payload mapping for local/Docker checks
config/ingest/pessoas.toml   Airtable PESSOAS mapping
docker/ingest/               Production API image and Compose deployment
docker/cdp/                  Standalone Chrome/CDP smoke image
.github/workflows/           Windows Receita workflow
experiments/                 Browser diagnostics and portal probes
tests/                       Unit and local integration tests
```

Run the complete test suite with `uv run python -m pytest tests -q`. The Airtable test
uses a local standard-library HTTP server, so it validates the real client and
provider PATCH/GET flow without modifying an Airtable base.
