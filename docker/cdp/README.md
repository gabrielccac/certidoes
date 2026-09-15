# Headed CDP smoke environment

This image runs the real Google Chrome binary under Xvfb, then uses the same
lean CDP profile flags as the local/garimpo flow.

Build from the project root:

```powershell
docker build -f docker/cdp/Dockerfile -t certidoes-cdp .
```

Smoke-check the image:

```powershell
docker run --rm --shm-size=2g certidoes-cdp python -c "from certidoes.browser import CdpChrome; print('cdp_import=passed')"
```

Run TRF1 with a persistent Docker profile and save the PDF to the local
`runs/trf1` directory:

```powershell
docker volume create certidoes-cdp-profile
docker run --rm --shm-size=2g `
  -v certidoes-cdp-profile:/app/experiments/profile `
  -v "${PWD}\runs:/app/runs" `
  certidoes-cdp python -m certidoes.sources.trf1 `
  --documento "124.333.776-17" --tipo-certidao-id 1 --orgao DF `
  --output-dir /app/runs/trf1 --profile /app/experiments/profile --debug-port 9334
```

Do not use the same profile volume concurrently from local Chrome and Docker.
