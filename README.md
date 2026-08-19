# O-RAN_CMF_NORI

**Conflict Mitigation Framework (CMF) for O-RAN, on top of NORI/ns-3.**

Este repositório reúne, em um único lugar, tudo o que é necessário para reproduzir o cenário de conflito entre xApps descrito em [`czezy/O-RAN_CMF_CM2023`](https://github.com/czezy/O-RAN_CMF_CM2023) (*"Conflict Mitigation Framework and Conflict Detection in O-RAN nRT-RIC"*, IEEE ComMag 2023) usando o módulo [NORI](https://github.com/lasseufpa/nori) do ns-3:

- uma simulação ns-3 (`nori-cmf.cc`) com 19 estações-base e 380 usuários, conectada de verdade a um Near-RT RIC via E2;
- dois xApps OSC que competem pelo controle da fronteira de handover de cada célula — **MRO** (Mobility Robustness Optimization) e **MLB** (Mobility Load Balancing);
- o Conflict Mitigation Framework embarcado na própria simulação, que detecta e (opcionalmente) resolve os conflitos entre as decisões dos dois xApps.

Nada aqui é um projeto novo do zero: são **patches** sobre dois projetos-base já existentes no ambiente OpenRAN@Brasil — o módulo `nori` do ns-3 e o xApp `xapp-nori`. Este README ensina exatamente onde aplicar cada patch e em que ordem.

## Sumário

- [Arquitetura](#arquitetura)
- [Estrutura deste repositório](#estrutura-deste-repositório)
- [Pré-requisitos](#pré-requisitos)
- [Passo a passo: clonar e aplicar os patches](#passo-a-passo-clonar-e-aplicar-os-patches)
  - [1. O módulo `nori-cmf` (ns-3)](#1-o-módulo-nori-cmf-ns-3)
  - [2. O xApp MRO](#2-o-xapp-mro)
  - [3. O xApp MLB](#3-o-xapp-mlb)
- [Compilando o cenário ns-3](#compilando-o-cenário-ns-3)
- [Embarcando os xApps no Near-RT RIC](#embarcando-os-xapps-no-near-rt-ric)
- [Executando o cenário completo](#executando-o-cenário-completo)
- [Verificando se o laço de controle está fechando](#verificando-se-o-laço-de-controle-está-fechando)
- [Problemas conhecidos](#problemas-conhecidos)
- [Referências](#referências)

## Arquitetura

```mermaid
flowchart LR
    subgraph ns3["ns-3 (nori-cmf.cc)"]
        BS["19 estações-base<br/>380 UEs"]
        CMF["Conflict Mitigation<br/>Framework (CMF)"]
        BS <--> CMF
    end

    subgraph ric["Near-RT RIC"]
        E2T["e2term"]
        SUB["submgr"]
    end

    subgraph xapps["xApps"]
        MRO["xApp MRO<br/>Hysteresis / TTT"]
        MLB["xApp MLB<br/>CIO"]
    end

    BS -- "E2 / SCTP<br/>indicação KPM" --> E2T
    E2T -- RMR --> MRO
    E2T -- RMR --> MLB
    MRO -- "RIC Control Request<br/>(HOHysteresis, HOTimeToTrigger)" --> E2T
    MLB -- "RIC Control Request<br/>(HOMeasurementOffset)" --> E2T
    E2T -- E2 / SCTP --> CMF
    MRO -.-> SUB
    MLB -.-> SUB
```

Os três parâmetros disputados — `HOMeasurementOffset` (CIO), `HOHysteresis` e `HOTimeToTrigger` — entram juntos na mesma condição do evento de handover A3. O MLB escreve o primeiro para balancear carga; o MRO escreve os outros dois para reduzir ping-pongs e falhas de enlace. Como pertencem ao mesmo grupo funcional (`CellAffectHandoverBoundary`), decisões simultâneas dos dois xApps sobre a mesma célula são um **conflito indireto** — exatamente o que o CMF do `nori-cmf.cc` foi construído para detectar e, dependendo do modo escolhido, mitigar.

Para o detalhamento completo do modelo de rede, rádio, mobilidade e do próprio CMF, veja [`nori-cmf/docs/nori-cmf.md`](nori-cmf/docs/nori-cmf.md). Para o funcionamento interno de cada xApp, veja o `README.md` dentro de [`xapp-MRO/`](xapp-MRO/README.md) e [`xapp-MLB/`](xapp-MLB/README.md).

## Estrutura deste repositório

```
O-RAN_CMF_NORI/
├── nori-cmf/
│   ├── examples/
│   │   ├── nori-cmf.cc                    # o cenário (arquivo novo)
│   │   └── CMakeLists.txt                 # registra o exemplo no build
│   ├── model/
│   │   ├── asn1c-types.cc / .h            # fix: RANParameterItem (double-free + m_id)
│   │   └── ric-control-message.cc         # fix: decodificação de ranParameters-List
│   ├── docs/nori-cmf.md                   # documentação completa do cenário
│   ├── run_nori_cmf.sh                    # script de execução
│   └── nori-cmf-examples-model.patch      # patch de examples/ e model/
├── xapp-MRO/
│   ├── ... (árvore completa do xApp já modificado)
│   └── xapp-MRO.patch                     # patch relativo ao xapp-nori original
└── xapp-MLB/
    ├── ... (árvore completa do xApp já modificado)
    └── xapp-MLB.patch                     # patch relativo ao xapp-nori original
```

Cada diretório carrega **duas formas equivalentes** do mesmo conteúdo: a árvore de arquivos já pronta (para quem só quer copiar e usar) e um `.patch` (para quem prefere aplicar as mudanças sobre um clone limpo do projeto-base — a forma recomendada, porque preserva o histórico git de cada projeto). Este guia usa os `.patch`.

## Pré-requisitos

- Uma VM do [OpenRAN@Brasil Blueprint v1](https://github.com/LABORA-INF-UFG/openran-br-blueprint/wiki/OpenRAN@Brasil-Blueprint-v1), com `git`, `kubectl`, `docker` e `dms_cli` disponíveis;
- Um Near-RT RIC (namespace `ricplt`) já implantado e saudável — confira com `kubectl get pods -n ricplt` (todos os pods em `1/1` ou `2/2`);
- Um registry Docker local acessível em `127.0.0.1:5001` (já vem provisionado no blueprint);
- `git apply` (ou `patch`) disponível no `$PATH` — ambos já vêm no blueprint.

Todos os comandos abaixo assumem que você está no `$HOME` da VM (`~`, ou seja, `/home/openran-br` no ambiente de referência).

## Passo a passo: clonar e aplicar os patches

A ideia geral, repetida três vezes, é sempre a mesma: **clonar o projeto-base intocado e aplicar o patch por cima**. Nenhum dos três patches modifica os projetos originais — eles só adicionam o que falta para o CMF funcionar.

### 1. O módulo `nori-cmf` (ns-3)

O patch `nori-cmf-examples-model.patch` se aplica sobre o **módulo `nori`** que já vive dentro do seu checkout do ns-3, em `ns-3-dev/contrib/nori`. Se você ainda não tem esse módulo, clone-o primeiro:

```bash
cd ~/ns-3-dev/contrib
git clone https://github.com/lasseufpa/nori.git   # pule se contrib/nori já existir
```

Agora aplique o patch:

```bash
cd ~/ns-3-dev/contrib/nori
git apply ~/O-RAN_CMF_NORI/nori-cmf/nori-cmf-examples-model.patch
```

Isso faz três coisas:

1. adiciona `examples/nori-cmf.cc` — o cenário completo (19 células, xApps emulados internamente, CMF);
2. registra o novo exemplo em `examples/CMakeLists.txt`, para o `./ns3 build` encontrá-lo;
3. corrige três bugs em `model/asn1c-types.{cc,h}` e `model/ric-control-message.cc` que, sem eles, fariam qualquer RIC Control Request de um xApp real ser silenciosamente ignorado (ou, em alguns casos, derrubar a simulação com um *double free*). Sem esse terceiro item, os xApps MRO e MLB deste repositório não conseguem controlar as células — mesmo se estiverem rodando perfeitamente.

Confira que aplicou certo:

```bash
git status --short
#  M examples/CMakeLists.txt
#  M model/asn1c-types.cc
#  M model/asn1c-types.h
#  M model/ric-control-message.cc
# ?? examples/nori-cmf.cc
```

Por fim, copie o script de execução e a documentação para onde forem mais convenientes (o script espera rodar a partir de `~/ns-3-dev`):

```bash
cp ~/O-RAN_CMF_NORI/nori-cmf/run_nori_cmf.sh ~/ns-3-dev/
mkdir -p ~/ns-3-dev/contrib/nori/docs
cp ~/O-RAN_CMF_NORI/nori-cmf/docs/nori-cmf.md ~/ns-3-dev/contrib/nori/docs/
```

### 2. O xApp MRO

O patch `xapp-MRO.patch` se aplica sobre um clone **limpo** do [`xapp-nori`](https://github.com/LABORA-INF-UFG/xapp-nori), colocado no diretório `~/xapp-MRO`:

```bash
cd ~
git clone https://github.com/LABORA-INF-UFG/xapp-nori.git xapp-MRO
cd xapp-MRO
git apply ~/O-RAN_CMF_NORI/xapp-MRO/xapp-MRO.patch
```

O patch reescreve a lógica de controle (`src/custom_xapp.py`, `src/main.py`), remove o módulo de RL que não é usado aqui (`src/env.py`), renomeia a instância para `xappmro` em todos os manifests e scripts (`init/config-file.json`, `setup.py`, `update_xapp.sh`, `log_xapp.sh`, `resubscribe.sh`), reduz `src/requirements.txt` às dependências realmente usadas, e substitui o `README.md` por um guia dedicado ao MRO.

Confira:

```bash
git status --short
#  M README.md
#  M init/config-file.json
#  M install_influx_grafana.sh
#  M log_xapp.sh
#  M redeploy_ric.sh
#  M resubscribe.sh
#  M setup.py
#  M src/custom_xapp.py
#  D src/env.py
#  M src/main.py
#  M src/requirements.txt
#  M update_xapp.sh
```

### 3. O xApp MLB

Exatamente o mesmo procedimento, em um clone separado, com o patch do MLB:

```bash
cd ~
git clone https://github.com/LABORA-INF-UFG/xapp-nori.git xapp-MLB
cd xapp-MLB
git apply ~/O-RAN_CMF_NORI/xapp-MLB/xapp-MLB.patch
```

O MLB é o par funcional do MRO: mesma estrutura de patch, mesma lista de arquivos alterados — a diferença está inteiramente na lógica de controle (`custom_xapp.py`), que aqui decide o `HOMeasurementOffset` (CIO) a partir da carga de PRBs da célula, em vez de Hysteresis/TTT.

> **Por que dois clones separados do mesmo repositório, em vez de um branch cada?** Porque `xapp-MRO` e `xapp-MLB` são publicados como dois xApps OSC independentes — cada um com seu próprio nome (`xappmro`/`xappmlb`), sua própria imagem Docker e seu próprio `Chart`/instalação no RIC (`dms_cli onboard`/`install`). Dois diretórios de trabalho separados evitam qualquer chance de misturar as instalações.

## Compilando o cenário ns-3

Com o patch do `nori-cmf` aplicado (passo 1), compile o módulo:

```bash
cd ~/ns-3-dev
./ns3 build nori-cmf
```

Na primeira vez que qualquer parte do módulo `nori` for tocada, o `ns3 build` (sem argumento) recompila o módulo inteiro — pode levar alguns minutos. Builds seguintes, tocando só em `nori-cmf.cc`, são rápidos.

Teste rapidamente em modo *offline* (sem depender do RIC — útil para confirmar que o build está saudável antes de mexer com Kubernetes):

```bash
./ns3 run "nori-cmf --useE2=0 --simTime=30 --cmMode=none"
```

Se aparecer o resumo `NORI CMF summary` no final, o build está correto.

## Embarcando os xApps no Near-RT RIC

Com o RIC saudável (`kubectl get pods -n ricplt`), instale os dois xApps — a ordem entre eles não importa:

```bash
cd ~/xapp-MRO && bash update_xapp.sh
cd ~/xapp-MLB && bash update_xapp.sh
```

Cada `update_xapp.sh` faz o ciclo completo sozinho: onboard do chart via `dms_cli`, build da imagem Docker, push para `127.0.0.1:5001`, e instalação no namespace `ricxapp`. Na primeira execução de cada um, o build da imagem baixa e compila `rmr`, clona o `ric-plt-xapp-frame-py` e instala as dependências Python — espere alguns minutos; execuções seguintes reaproveitam o cache de camadas do Docker.

```bash
kubectl get pods -n ricxapp
# ricxapp-xappmro-... 1/1 Running
# ricxapp-xappmlb-... 1/1 Running
```

Cada `xapp-*/README.md` traz um roteiro passo a passo bem mais detalhado desta etapa, incluindo diagnóstico de `CrashLoopBackOff` e de reinícios inesperados — vale a leitura antes de rodar em um cluster que você não conhece bem.

## Executando o cenário completo

Com os xApps já `1/1 Running`, descubra o IP do pod `e2term` e suba a simulação em modo E2:

```bash
E2TERM_IP=$(kubectl get pods -n ricplt -o wide | grep e2term-alpha | awk '{print $6}')
cd ~/ns-3-dev
./ns3 run "nori-cmf --useE2=1 --ipE2TermRic=$E2TERM_IP"
```

As 19 células devem completar o E2-SETUP com o RIC (`[INFO ] [SCTP] Sent E2-SETUP-REQUEST` no log da simulação; `CONNECTED` nos logs do `e2mgr`).

Como os xApps normalmente já estavam rodando **antes** dessas 19 células existirem, é preciso pedir a eles para procurar de novo os nós E2 disponíveis:

```bash
MRO_IP=$(kubectl get svc -n ricxapp service-ricxapp-xappmro-http -o jsonpath='{.spec.clusterIP}')
MLB_IP=$(kubectl get svc -n ricxapp service-ricxapp-xappmlb-http -o jsonpath='{.spec.clusterIP}')
curl "http://$MRO_IP:8080/ric/v1/resubscribe"
curl "http://$MLB_IP:8080/ric/v1/resubscribe"
```

(Os scripts `bash resubscribe.sh` dentro de cada diretório do xApp fazem a mesma coisa, resolvendo o Service automaticamente.)

## Verificando se o laço de controle está fechando

Acompanhe os logs de qualquer um dos dois xApps:

```bash
kubectl logs -n ricxapp -f $(kubectl get pods -n ricxapp | grep xappmro | awk '{print $1}') | grep "Cell NRCellDU"
```

Uma decisão real e aplicada tem esta cara — repare que o `(was ...)` de uma linha bate com o valor que a linha *anterior*, para a mesma célula, mandou aplicar:

```text
Cell NRCellDU_12: ho=11 pp_ratio=27.3% rlf_ratio=9.1% -> hysteresis=1.0dB ttt=0.1s (was hysteresis=1.0 ttt=0.08)
Cell NRCellDU_12: ho=12 pp_ratio=25.0% rlf_ratio=8.3% -> hysteresis=1.0dB ttt=0.08s (was hysteresis=1.0 ttt=0.1)
```

Isso confirma o laço completo: a decisão do xApp saiu como RIC Control Request, foi decodificada e aplicada pelo `nori-cmf.cc` (graças às correções do passo 1), e voltou refletida na indicação KPM seguinte. Os mesmos valores também aparecem no CSV que a própria simulação grava (`<outputDir>/bs-12.csv`, colunas `hyst`/`ttt`, ou `cio` para o MLB) — útil para conferência cruzada sem depender dos logs do xApp.

Se ambos os xApps estiverem ativos ao mesmo tempo, acompanhe também `conflicts.json` dentro do diretório de saída da simulação: cada conflito indireto detectado entre MRO e MLB é registrado ali, no mesmo formato JSON do artigo de referência.

## Problemas conhecidos

- **`e2term` pode crashar ao final de uma simulação.** Quando o `nori-cmf` termina, as 19 conexões SCTP fecham quase ao mesmo tempo; isso expõe um bug pré-existente no `e2term` do RIC (`free(): invalid pointer`, não relacionado a este repositório) que derruba o pod com alguma frequência. O Kubernetes reinicia o pod sozinho em 1–2 minutos — espere `kubectl get pods -n ricplt` mostrar `1/1` de novo antes de rodar outra simulação.
- **Resubscrição pode demorar em clusters com histórico.** `bash resubscribe.sh` retorna imediatamente (o trabalho roda em segundo plano dentro do pod), mas, se o RIC acumulou muitos registros de gNBs antigos/obsoletos ao longo do tempo, o ciclo completo de cancelar+recriar assinaturas pode levar mais de um minuto. Acompanhe os logs em vez de assumir falha.

O `README.md` de cada xApp traz uma seção de troubleshooting mais completa, incluindo como diagnosticar `CrashLoopBackOff` e reinícios por falha do *liveness probe* (exit code 137).

## Referências

- Especificação do cenário: [`czezy/O-RAN_CMF_CM2023`](https://github.com/czezy/O-RAN_CMF_CM2023)
- Módulo NORI: [`lasseufpa/nori`](https://github.com/lasseufpa/nori)
- xApp base: [`LABORA-INF-UFG/xapp-nori`](https://github.com/LABORA-INF-UFG/xapp-nori)
- Documentação completa do cenário: [`nori-cmf/docs/nori-cmf.md`](nori-cmf/docs/nori-cmf.md)
- 3GPP TR 38.901 (modelo de canal UMa) e TS 38.331 (parâmetros de handover: CIO, histerese, TTT)
