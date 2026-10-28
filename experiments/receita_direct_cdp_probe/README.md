# Comparação Android: SeleniumBase e CDP independente

O fluxo de referência é o commit `af6af04828651a461120cfce379c39685edb1b27`,
validado anteriormente em 12 casos. `reference.py` preserva esse fluxo e altera
somente a construção do cliente, por `chrome_factory`. O cliente independente
usa `websocket-client==1.9.2` e `mycdp==1.4.0` para comandos/tipos do protocolo.
Não utiliza o runtime do SeleniumBase, Selenium ou Playwright.

## Diagnóstico reproduzido antes da repetição

O primeiro teste direto falhou antes do formulário com `ValueError`. Em Chrome
133.0.6943.141 local, numa página sintética, foi reproduzida a exceção:
`None is not a valid LocalNetworkAccessRequestPolicy`, ao decodificar
`Network.requestWillBeSentExtraInfo`. O esquema mycdp exige um campo não enviado
por essa versão do navegador. O adaptador decodificava inclusive eventos sem
assinantes, e a exceção interrompia a leitura da resposta do comando corrente.

A correção seleciona os eventos assinados antes da decodificação. Erros de
decodificação em eventos necessários continuam explícitos, com método e tipo do
erro. O teste de regressão reproduz a incompatibilidade e garante que ela não
interrompa comandos quando o evento não é utilizado.

## Verificações e emissão

- Oito testes locais passaram, incluindo correlação de respostas, eventos,
  exceções, navegação e bloqueio da Receita quando a checagem inicial falha.
- Páginas de controle em Chrome desktop 133 e 155 validaram navegação, digitação,
  clique, POST, eventos, leitura do corpo e avaliação assíncrona.
- Antes dos casos reais, o Chrome Android 133.0.6943.137 passou pela mesma
  checagem, usando toque CDP e uma página HTTP sintética acessível pelo host do
  emulador. Essa etapa é obrigatória: sua falha encerra o job antes da Receita.
- Os dois casos reais usam o mesmo worker Android 16/API 36, configurações do
  AVD de referência, perfil Chrome limpo por caso, pedido e SDK original iguais.
  Cada controlador roda em seu próprio ambiente Python isolado.

[Run aprovado 40760437413](https://github.com/gabrielccac/certidoes/actions/runs/40760437413/job/123070595235),
commit `7bf7d4528dfdaf6bd2fc7733814b4c163e873e6a`, branch
`test/receita-direct-cdp-9c36b7d5ca`:

| Caso | Resultado | PDF | Tempo do caso | Pacotes instalados | Pico RSS Python |
| --- | --- | --- | --- | --- | --- |
| SeleniumBase 4.56.4 | sucesso | 77.072 bytes, 1 página, CPF conferido | 28,98 s | 126,0 MiB | 125,6 MiB |
| CDP independente | sucesso | 77.072 bytes, 1 página, CPF conferido | 20,67 s | 64,7 MiB | 103,3 MiB |

Ambos registraram HTTP 200 na validação e na emissão. O caso independente
registrou `seleniumbase_installed=false`, `seleniumbase_imported=false` e
`playwright_imported=false`. O PDF independente tem SHA-256
`0d2c9713f9731b6e4a6ed86537e64f35adb258c040b0cc32ceff8ea9e1484f7d`.

Os tamanhos são a soma lógica dos arquivos de site-packages após executar o
caso. A memória é somente do processo Python; não inclui Android ou Chrome.
Os tempos são observações de uma comparação, com controle executado primeiro,
não um benchmark nem prova de ganho estável. Há um sucesso E2E da versão direta
corrigida; sua repetibilidade, integração em produção e o login do Meu INSS
ainda não foram validados por este teste.

Evidências locais: `runs/receita-local/direct-cdp-probe/connection-check/` e
`runs/receita-local/direct-cdp-probe/connection-fixed/`. O artifact contém
somente relatórios; PDFs, tokens e dados pessoais não são publicados.

## Docker local: emissão aceita

O teste local também passou com o emulador Android e o controlador Python
dentro do mesmo container Docker. A imagem `receita-android-cdp-direct:local`
usa Python 3.12.15, sem SeleniumBase, Selenium ou Playwright; o SDK é montado
somente para leitura e `/dev/kvm` é exposto ao container. Não usa Xvfb, modo
privilegiado, Chrome do host nem portas CDP publicadas no host.

Android 16/API 36, Chrome 133.0.6943.137 e emulador 37.2.12.0. O boot levou
82,06 s; a checagem sintética de CDP passou antes da Receita. O caso direto
levou 35,60 s, registrou HTTP 200 na validação e na emissão e gerou um PDF de
77.072 bytes, uma página, CPF conferido. O arquivo foi analisado novamente no
host, com o mesmo SHA-256:
`45ad4de9bfc7d0280421d18546acebf58c5e6b6ccd0712a39af2a65463a92c98`.

A checagem inicial da imagem identificou `libxkbfile.so.1` ausente antes de
iniciar o emulador. A dependência `libxkbfile1` foi adicionada ao Dockerfile
compartilhado e o binário/KVM foram validados antes da execução E2E. O launcher
aceita `--direct-only` para dispensar o ambiente de controle SeleniumBase, e o
caso aceita entrada local por variáveis, sem imprimir identidade fora do Actions.
Nove testes locais passaram após essas adaptações.

Evidências e PDF:
`runs/receita-local/direct-cdp-probe/docker-local/run-a43110d0/attestation.json`
e `runs/direct-cdp-comparison/direct/certificate.pdf` nesse diretório. O
container do teste e seus dados de AVD foram removidos após preservar os
relatórios e o PDF. O SDK original e os demais containers foram preservados.

É uma execução Docker bem-sucedida, com KVM disponível. Ainda não comprova
consistência de um serviço persistente nem execução em servidor sem KVM.

## Reproduzir o container

O `Dockerfile` fixa o ambiente Python do teste. `docker_boot.py` inicia um AVD
`test` descartável em `/work/avd` e executa por padrão `android --direct-only`.
Também aceita um comando Python como argumentos para testar outro portal.
Monte este repositório em `/app` (somente leitura), o SDK/API 36 em
`/opt/android-sdk` (somente leitura), uma pasta de saída em `/work` e exponha
`/dev/kvm`. Prepare o AVD Pixel 6 com 2 CPUs, 3072 MB de RAM e disco de 4096 MB.
Use usuário não root, `--cap-drop ALL`, `--security-opt no-new-privileges`,
`--cpus 3 --memory 5g --memory-swap 7g --shm-size 1g --pids-limit 512`.
Execute `/opt/direct/bin/python /app/experiments/receita_direct_cdp_probe/docker_boot.py`.
Forneça `LIVE_DOCUMENT` e `LIVE_BIRTH` pelo ambiente, nunca por argumentos
nem arquivos versionados. O SDK e os dados do AVD não estão incluídos na imagem.
