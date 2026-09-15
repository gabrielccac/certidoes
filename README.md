# Certidões

Sistema para gerar certidões da Receita Federal, TJDFT, TRF1, Fazenda DF,
TRT10 e TST. A API de ingestão recebe uma solicitação, executa as fontes
configuradas, salva os PDFs em `runs/` e publica um resumo sem dados pessoais.

## Requisitos

- Python 3.12 e [uv](https://docs.astral.sh/uv/) para desenvolvimento local.
- Docker Desktop ou Docker Engine para a imagem completa de ingestão.
- Um endereço HTTPS público quando o Airtable precisar buscar os anexos.
- Um token pessoal fine-grained do GitHub para o executor da Receita.
- Um token pessoal do Airtable quando o provedor Airtable for utilizado.

A imagem contém Google Chrome, Xvfb, Playwright, OpenCV, ddddocr, o arquivo
`solved.png` e o volume de perfil CDP persistente usado pelos portais. O
Playwright é usado pelas fontes que dependem de navegador; ele não é um
serviço separado.

## Configuração local

```powershell
uv sync
uv run python -m pytest tests -q
```

Para iniciar a API usando o provedor inline, sem acesso ao Airtable:

```powershell
$env:INGEST_SECRET = "segredo-local"
$env:PUBLIC_BASE_URL = "http://127.0.0.1:8090"
uv run python -m certidoes.ingest.worker `
  --config config/ingest/local.toml --serve --port 8090
```

Envie o payload de teste:

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
  -Headers @{"X-Ingest-Secret" = "segredo-local"} `
  -ContentType "application/json" -Body $body
```

O provedor local grava um relatório sem dados pessoais e não altera o
Airtable. A execução local da Receita ainda depende do hCaptcha no navegador
Windows/GitHub; para reproduzir o caminho completo use o Docker com o executor
GitHub descrito abaixo.

## Execução com Docker

Copie `.env.example` para `.env` e preencha os valores necessários. O arquivo
é lido quando o container inicia e é excluído do contexto de build da imagem.

```powershell
Copy-Item .env.example .env
docker compose --env-file .env -f docker/ingest/compose.yml up --build -d
docker compose --env-file .env -f docker/ingest/compose.yml logs -f api
```

A API fica disponível através do Caddy em `INGEST_DOMAIN`. O DNS desse
hostname precisa apontar para o servidor Docker para que o Caddy possa emitir
o certificado HTTPS. A API espera `POST /jobs` com o header
`X-Ingest-Secret`; `GET /healthz` é o endpoint de saúde.

O perfil do Chrome e os arquivos gerados ficam em volumes nomeados do Docker.
Não monte o mesmo perfil simultaneamente no Chrome local e no container.

Para processar um registro real do Airtable, envie somente o ID do registro:

```json
{
  "provider": "airtable",
  "ref": {"record_id": "recXXXXXXXXXXXXXX"}
}
```

O mapeamento em `config/ingest/pessoas.toml` usa os IDs estáveis dos campos do
Airtable, altera `PENDENTE` ou `TENTAR NOVAMENTE` para `PROCESSANDO` e termina
com `FINALIZADO` ou `FALHA`, incluindo o resumo e os URLs dos anexos. Antes de
usar uma base ou tabela nova, execute a verificação somente leitura:

```powershell
uv run python -m certidoes.ingest.worker `
  --config config/ingest/pessoas.toml --check --record recXXXXXXXXXXXXXX
```

## Variáveis do `.env`

| Variável | Onde obter ou como definir |
| --- | --- |
| `AIRTABLE_PAT` | Airtable Developer Hub → Personal access tokens. Conceda `data.records:read` e `data.records:write`; adicione `schema.bases:read` para obter a verificação exata do comando `--check`. |
| `AIRTABLE_BASE_ID` | Identificador `app...` na URL da base do Airtable. |
| `INGEST_SECRET` | Um segredo aleatório para a API. Gere com `python -c "import secrets; print(secrets.token_urlsafe(32))"`. |
| `PUBLIC_BASE_URL` | URL HTTPS pública que o Airtable usará para buscar `/files/...`; não pode ser `localhost`. |
| `INGEST_DOMAIN` | Hostname usado pelo Caddy, normalmente o mesmo host de `PUBLIC_BASE_URL`. |
| `RECEITA_EXECUTOR` | Use `github` no Docker/produção. `local` usa o navegador local e serve principalmente para diagnóstico. |
| `RECEITA_GITHUB_REPO` | Repositório no formato `organização/nome`, normalmente `gabrielccac/certidoes`. |
| `RECEITA_GITHUB_TOKEN` | Token pessoal fine-grained do GitHub, limitado a este repositório e com `Actions: Read and write`. Ele é injetado em tempo de execução e não entra no Dockerfile. |
| `RECEITA_GITHUB_REF` | Branch ou tag que contém o workflow; use `main` em produção. |

### Token do GitHub

Crie o token em
<https://github.com/settings/personal-access-tokens/new>.

1. Em **Resource owner**, selecione `gabrielccac`.
2. Em **Repository access**, escolha **Only select repositories**.
3. Selecione `certidoes`.
4. Em **Repository permissions → Actions**, escolha **Read and write**.
5. Gere o token e coloque apenas o valor no `.env`:

```text
RECEITA_GITHUB_TOKEN=github_pat_...
```

Esse token é um PAT (personal access token) do tipo fine-grained. Ele não deve
ser colocado no Dockerfile, no código ou em um secret do workflow. O secret do
workflow é outra credencial: `RECEITA_PAYLOAD_PRIVATE_KEY`, configurada nas
secrets do repositório GitHub. Ela permite descriptografar o payload enviado
pela API e nunca deve ser copiada para o `.env`.

A chave pública correspondente já está em
`src/certidoes/receita_github_public.pem`. O workflow recebe o CPF e a data de
nascimento de forma criptografada, executa no worker Windows, gera um artefato
temporário e o API baixa, valida a assinatura `%PDF-`, salva localmente e
remove o artefato. O artefato é transporte temporário, não armazenamento
permanente.

## Estrutura do projeto

```text
src/certidoes/sources/       Um adapter para cada portal de certidões
src/certidoes/ingest/        API, providers, mapeamento, anexos e worker
src/certidoes/registry.py    Registro usado pelos mapeamentos TOML
config/ingest/local.toml     Mapeamento para payload inline/local
config/ingest/pessoas.toml   Mapeamento da tabela PESSOAS do Airtable
docker/ingest/               Imagem da API e deployment com Compose
docker/cdp/                  Imagem independente para Chrome/CDP
.github/workflows/           Workflow Windows da Receita
experiments/                 Diagnósticos de navegador e testes de portais
tests/                       Testes unitários e integração local
```

Execute a suíte completa com:

```powershell
uv run python -m pytest tests -q
```

O teste do Airtable inicia um servidor HTTP local da biblioteca padrão. Assim,
ele valida o cliente e o provider reais, incluindo `GET`, claim para
`PROCESSANDO`, `PATCH` final e URLs de anexos, sem alterar nenhuma base real.
