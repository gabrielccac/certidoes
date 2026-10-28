# Diagnóstico da Receita em execução remota

O objetivo é emitir a certidão em infraestrutura remota, sem depender do computador
do usuário. A aprovação exige PDF válido, CPF conferido no conteúdo e repetição
em três jobs remotos novos. Aprovar apenas o token ou executar um runner neste
desktop não atende ao objetivo.

## Evidências disponíveis

| Evidência | Conclusão permitida |
| --- | --- |
| Desktop local com perfil novo emite o PDF | Perfil antigo não é necessário para o sucesso local |
| Desktop local com WebGL desabilitado emite o PDF | Disponibilidade de WebGL não é requisito absoluto no local |
| Xvfb local com o mesmo CdpChrome falha | Trocar apenas o iniciador não resolveu esse caminho |
| Windows hospedado falha com Chrome 155 e 156 no mesmo worker | A diferença de versão não explica sozinha os resultados |
| Windows hospedado falha com Chrome padrão e SeleniumBase | Essa troca de biblioteca não resolveu a rejeição |
| Cloak 146 falhou nos testes Windows e Linux anteriores | Esse binário/configuração não é solução comprovada; Cloak 155 permanece sem teste |
| Runner local acionado pelo GitHub passou após tratar aviso de cookies | O fluxo pode ser automatizado nesse desktop; não comprova deploy remoto |

Os relatórios anteriores estão em `runs/receita-local/`. As respostas atuais da
Receita expõem `CaptchaFalhaValidacao`, sem o motivo interno da validação. IP,
GPU, sistema operacional ou reputação continuam hipóteses, não causas comprovadas.

## Variáveis e testes

| Grupo | O que verificar | Controle e decisão |
| --- | --- | --- |
| Entrada | Documento, data de nascimento, tipo de pessoa e normalização | Mesmo pedido criptografado; registrar apenas igualdade, nomes de campos e comprimentos |
| Código | Commit, launcher, argumentos e versão dos pacotes | Fixar e registrar versões; variar uma opção por caso |
| Navegador | Chrome estável, Chrome for Testing, Chromium e alternativas | Começar com o Chrome que já emitiu; não atribuir resultado de uma versão a todas |
| Sistema | Windows, Linux e macOS | Mesmo fluxo oficial; mudança de SO serve primeiro para localizar uma plataforma remota viável |
| Interface | Headed, headless, Xvfb e backend gráfico | Testar headed/headless no mesmo tipo de worker; registrar foco e visibilidade reais |
| Desktop | Sessão interativa, foco, janela ativa e resolução | Medir antes do formulário e da validação; comparar `bring_to_front` com o controle |
| Formulário | Aviso de cookies, hidratação, máscaras e valores que são apagados | Conferir igualdade dos campos antes do envio, sem guardar os valores |
| Carregamento | DOM pronto, widgets prontos e execução prematura | Medir tempos e testar espera por estado funcional em vez de pausas arbitrárias |
| Widgets | Quantidade, sitekey, widget usado pelo formulário e token enviado | Comparar token com respostas dos widgets em memória; nunca gravar token |
| Token | Geração, idade, expiração e uso único | Registrar tempos; cada tentativa deve obter token novo no mesmo navegador |
| Fluxo HTTP | Validação e emissão, corpo, cabeçalhos e cookies | Usar a aplicação oficial na mesma sessão; comparar campos/headers permitidos e códigos de resposta |
| Sessão | Perfil limpo, cookies, armazenamento e cache | Perfil dedicado por caso; não transferir perfil pessoal nem reaproveitar token |
| Observação | Efeito de diagnóstico CDP, avaliação e capturas | Comparar execução normal com diagnóstico adicional desativado |
| Rede | Região de saída, ASN, IPv4/IPv6, proxy e origem da conexão | Registrar país/ASN quando disponíveis; nunca tratar país ou IP como causa sem teste de rede controlado |
| DNS e TLS | Resolução do portal, servidor atendente e HTTP negociado | Usar o navegador para todas as chamadas; guardar apenas metadados permitidos |
| Relógio | Hora do sistema, data do servidor, fuso e idiomas | Medir diferença de relógio; testar configuração legítima de idioma/fuso separadamente |
| Recursos | CPU, memória, fontes, canvas e WebGL | Medir capacidades reais; ausência de um recurso não prova rejeição por esse recurso |
| CI | Usuário, ambiente do job, sandbox e instalação efetiva | Registrar metadados não secretos; confirmar sessão e executável usados |
| Captcha visual | Exibição, desafio de grade/arraste, modelo e respostas | Distinguir ausência de rodada de sucesso do solver; preservar erro de configuração |
| Repetição | Ordem, intervalos, tentativas e variação do serviço | Uma tentativa não mede estabilidade; repetir candidato bem-sucedido em jobs independentes |
| Artefato | Base64, assinatura PDF, conteúdo e documento solicitado | Aceitar somente PDF analisável e CPF correto |
| Integração | Dispatch, criptografia, artifact, download e limpeza | Depois da emissão remota, testar o executor GitHub real e seu transporte do PDF |

## Ordem de execução

1. Instrumentar o mesmo `receita.generate()` com relatório redigido, sem substituir
   o formulário por execução manual de outro widget.
2. Comparar headed/headless, foco/visibilidade e macOS remoto. Incluir Linux
   headless sem Xvfb. Manter versões, pedido e perfil dedicado identificados.
3. No ramo que falhar, separar campos apagados/overlay, falta de validação e
   rejeição HTTP explícita. Um timeout de formulário não é prova de rejeição do token.
4. Testar foco, espera de prontidão e ausência de diagnóstico individualmente
   onde os relatórios apontarem diferenças. Idioma/fuso e rede seguem conforme evidência.
5. Se necessário, comparar outra rede/servidor remoto disponível. Acesso de saída
   via computador do usuário não será aceito como deploy final.
6. Ao obter um candidato, repetir em três workers novos. Só então integrar a
   configuração mínima e validar geração, transporte e download pela API existente.

## Escopo da implementação

Os pontos compartilhados são `src/certidoes/sources/receita.py`,
`src/certidoes/browser/cdp.py`, o workflow da Receita e seus testes. As demais
fontes de certidões ficam fora do escopo. Experimentos podem substituir o
launcher dentro do processo de teste; o backend de produção só muda após prova
de emissão remota e verificação dos chamadores.

## Proteção dos relatórios

Não publicar CPF, data de nascimento, token, cookies, chaves privadas, cabeçalhos
de autenticação, corpo completo de respostas ou HAR sem redação. Screenshots de
falha devem mascarar entradas do formulário. Os relatórios públicos contêm
igualdades, comprimentos, configurações não secretas, tempos e códigos de erro.

## Referências

- [Validação no servidor do hCaptcha](https://docs.hcaptcha.com/#verify-the-user-response-server-side)
- [Execução Playwright em CI](https://playwright.dev/python/docs/ci)
- [Workers hospedados pelo GitHub](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
