# Receita padrão: Redroid Android 15 + CDP direto

`RECEITA_EXECUTOR=redroid` é o padrão de `generate()`, do CLI e do Compose de
ingestão. Usa o fluxo que emitiu a certidão na VPS em 2026-10-29, sem KVM,
SeleniumBase, Playwright ou solver na execução da Receita. As demais fontes
continuam usando seus controladores existentes.

## Preparação

O host precisa de Linux x86_64, Docker e suporte a Binder, memfd, IPv6 e
DMA-BUF Heaps, com páginas de 4 KB. Na VPS Ubuntu 24.04, o kernel existente
6.8.0-147 já tinha essas opções; foi instalado apenas o pacote de módulos:

```sh
sudo apt-get install linux-modules-extra-$(uname -r)
sudo modprobe binder_linux devices=binder,hwbinder,vndbinder
```

O serviço Redroid exige container privilegiado. ADB e CDP ficam somente na
rede Docker, sem portas publicadas. Para reinicializações do servidor,
configure o carregamento de `binder_linux` no boot conforme sua distribuição.

As imagens oficiais Redroid não incluem Chrome. Extraia os APKs do SDK
Google APIs Android 36 x86_64 revisão 7 que foi utilizado nos testes:

```sh
python3 docker/receita/prepare_apks.py \
  --system-image /caminho/sdk/system-images/android-36/google_apis/x86_64/system.img
```

A extração é offline, não usa emulador nem KVM, requer `debugfs` do pacote
`e2fsprogs` e aproximadamente 3 GB temporários. Confere os SHA-256 dos dois
APKs Chrome 133.0.6943.137 antes de gravá-los em `docker/receita/apks/`.
APKs e dados Android não são versionados. O Dockerfile fixa a imagem Redroid
15 pelo digest. A imagem 16 testada falhou antes do portal por incompatibilidade
ashmem/memfd com o kernel; não é o padrão.

Depois siga o Compose de ingestão do README principal. Ele inicia o Android,
aguarda o boot e conecta a API pelo ADB. Na primeira emissão, instala os APKs
incluídos na imagem Android. O Android permanece disponível entre pedidos;
cada pedido recebe um perfil Chrome limpo.

## Execução e isolamento

O controlador precisa do comando `adb` no PATH. A imagem de ingestão já o
instala. `RECEITA_ANDROID_SERIAL` usa `receita-android:5555` por padrão;
`RECEITA_ANDROID_CDP_PORT` usa `9229`. Um lock em `runs/.receita-android.lock`
serializa os pedidos; compartilhe esse arquivo entre workers que atendem o
mesmo dispositivo. O prazo de aquisição é `request_timeout_s`; se ocupado,
retorna `android_ocupado` sem tocar no perfil do outro pedido.

Ao concluir, encerra e limpa o Chrome e remove o encaminhamento CDP. O PDF
é conferido contra o documento solicitado antes de retornar `CertificateResult`.
O diagnóstico `receita-android.json` contém estados e contadores sem CPF,
data de nascimento, cookies ou tokens. Um desafio visual encerra a tentativa;
o padrão não chama solver nem troca automaticamente para outro executor.

Para escolher explicitamente o controlador anterior, use
`RECEITA_EXECUTOR=local` ou `RECEITA_EXECUTOR=github`. Os testes antigos de
desktop e o workflow de diagnóstico continuam selecionando `local`.

## Evidência e limites

Na VPS, o Android iniciou em 29,79 s sem `/dev/kvm`. Chrome 133 nativo x86_64
passou na checagem sintética de navegação, digitação, toque, POST e resposta.
O caso Receita registrou HTTP 200 em verificar/Emissao e emitiu PDF de 77.110
bytes, uma página e CPF conferido em 10,46 s. O PDF foi conferido novamente
no host. Esse resultado é uma execução aceita, não uma medição de consistência
em carga. Não valida Redroid ARM nem a ausência de captcha no Meu INSS.
