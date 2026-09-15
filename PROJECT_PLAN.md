# Certificate project plan

## Decisions

- Keep one Python package and one result/error contract.
- Do not use a `BaseCrawler` class. Each source exports a typed request and a
  `generate(...)` function; a small registry is enough for dispatch.
- Use the lightest proven transport per source. Browser code is an adapter,
  not the place where business responses are interpreted.
- Keep the current `experiments/` directory until every source passes its live
  contract test, then remove only duplicated experiment code.
- Keep personal live fixtures outside Git. Commit only synthetic invalid data
  and redacted response fixtures.

## Source implementation map

| Source | Transport | Flow to retain |
| --- | --- | --- |
| TJDFT | HTTP | person lookup -> request -> PDF download |
| TRF1 | shared Chrome/CDP + HTTP | load challenge metadata/token -> certificate API |
| Receita PF/PJ | shared Chrome/CDP + HTTP | hCaptcha token -> verify -> existing-certificate or emission route |
| Fazenda DF | shared Chrome/CDP | fill form -> Turnstile state -> submit -> PDF/error modal |
| TRT10 | Playwright + HTTP precheck | validate -> UI emission -> CDP print-to-PDF |
| TST | HTTP first | JSF state/cookies -> CAPTCHA -> submit -> PDF; Playwright only if HTTP proves unstable |

Receita PF and PJ belong in one module because the endpoints and flow are the
same; contributor type and challenge page differ.

## Proposed layout

```text
pyproject.toml
src/certidoes/
  __init__.py
  errors.py             # one structured CertificateError
  models.py             # CertificateResult and shared document helpers
  registry.py           # source name -> generate function
  browser/
    cdp.py               # validated Garimpo-style shared profile helper
  sources/
    tjdft.py
    trf1.py
    receita.py
    fazenda.py
    trt10.py
    tst.py
tests/
  unit/                  # validation and response parsing; no network
  contract/              # recorded/redacted upstream responses
  live/                  # opt-in, sequential, never CI-default
experiments/             # retained until migration is proven
```

Do not add generic HTTP clients, repository layers, factories, or source base
classes unless repetition appears during the migration.

## Stable boundary

Every successful generator returns:

```python
CertificateResult(source, document_type, filename, pdf_path, metadata={})
```

Every expected failure raises one `CertificateError` carrying:

```text
source, stage, code, message, retryable, upstream_status
```

Stages are `input`, `challenge`, `validation`, `request`, and `artifact`.
The API/worker boundary converts that exception into its public response. Source
modules do not return incompatible `success`/`error`/`exception` dictionaries.

## Test strategy

### Unit and contract tests

- Validate required fields, CPF/CNPJ checksums, enums, and normalization.
- Parse every known success, business-error, challenge-error, malformed JSON,
  HTML error, missing-PDF, and processing response from redacted fixtures.
- Mock only the transport boundary; assert request bodies, headers, cookies,
  and state transitions.
- Validate PDF artifacts by `%PDF-` signature and non-trivial byte length.

### Opt-in live tests

- Run sequentially because CDP sources share one Chrome profile.
- Require `CERTIDOES_LIVE_DATA` pointing to an ignored local JSON file and an
  explicit `--live` flag.
- Never log documents, birth dates, names, tokens, cookies, or PDF contents.
- Save outputs under an ignored run directory and emit a redacted JSON report.
- Rate-limit challenge retries and cap every live test.

### Invalid-stage coverage

| Source | Safe live failures to exercise before a valid generation |
| --- | --- |
| TJDFT | malformed input locally; first-name mismatch; mother-name mismatch |
| TRF1 | unsupported type/organ locally; invalid or expired token; unknown document |
| Receita | birth-date mismatch; invalid/expired hCaptcha; processing/no-PDF response |
| Fazenda | missing required field; unsolved/expired Turnstile; invalid document; error modal; download timeout |
| TRT10 | invalid criterion locally; invalid document precheck; UI error; render/print timeout |
| TST | wrong CAPTCHA; invalid document; stale JSF state/token; missing PFNC/PDF |

Protocol and artifact failures should be fixture-driven rather than induced
against government services.

## Available live data

The reviewed repositories contain one checksum-valid CPF candidate and the
associated birth date, first name, and mother name. Without committing or
printing it, this can cover the PF happy path for TJDFT, TRF1, Receita,
Fazenda, TRT10, and TST.

Gaps:

- no checksum-valid CNPJ candidate;
- no trusted Fazenda vehicle or property fixture (one legacy numeric hint is
  present but unverified);
- no TRT name or CNPJ-root fixture;
- Receita's existing-certificate branch depends on current upstream state and
  cannot be guaranteed before the live run.

Checksum validity is not proof that upstream identity data still matches. The
first live phase should validate only, and issuance should be an explicit
second command.

## TST CAPTCHA replacement experiment

Gemini is used only for TST's small alphanumeric image CAPTCHA. Trial
`ddddocr` first: initialize one CPU model per process, restrict output to
lowercase letters and digits, and allow a fresh CAPTCHA retry.

Measure it without issuing a certificate by submitting a syntactically shaped
but invalid document. A correct CAPTCHA reaches the document-validation error;
an incorrect answer reaches the CAPTCHA error. Record first-pass accuracy,
three-attempt success rate, and latency over a small rate-limited sample.

If the generic model is insufficient, try OpenCV preprocessing plus the same
model. Train a site-specific ONNX model only after a labeled dataset justifies
that work. Do not add PaddleOCR/EasyOCR unless the CAPTCHA-specific model loses
the benchmark; they solve a broader and heavier problem.

## Implementation order

1. Add package skeleton, shared models/errors, and migrate the validated CDP helper.
2. Migrate TJDFT and TRT10 to establish HTTP and Playwright patterns.
3. Migrate TRF1, Receita, and Fazenda using the shared CDP profile.
4. Run the TST offline-OCR experiment, then migrate its HTTP flow.
5. Add redacted contract fixtures and the invalid-stage matrix source by source.
6. Run validation-only live cases, then one explicit happy-path generation per source.
7. Add the external API/worker wrapper after source contracts are stable.
