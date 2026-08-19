# `nori-cmf.cc` — Cenário de RAN e E2 para o Conflict Mitigation Framework do O-RAN

## Sumário

1. [Visão geral](#1-visão-geral)
2. [Contexto: o repositório de referência](#2-contexto-o-repositório-de-referência)
3. [O problema: conflitos entre xApps](#3-o-problema-conflitos-entre-xapps)
4. [Arquitetura da simulação](#4-arquitetura-da-simulação)
5. [O cenário de rede](#5-o-cenário-de-rede)
6. [Modelo de rádio](#6-modelo-de-rádio)
7. [Mobilidade e tráfego](#7-mobilidade-e-tráfego)
8. [Handover A3 e o laço de controle](#8-handover-a3-e-o-laço-de-controle)
9. [Os xApps MRO e MLB](#9-os-xapps-mro-e-mlb)
10. [O Conflict Mitigation Framework (CMF)](#10-o-conflict-mitigation-framework-cmf)
11. [Integração com o Near-RT RIC via E2](#11-integração-com-o-near-rt-ric-via-e2)
12. [Arquivos de saída](#12-arquivos-de-saída)
13. [Parâmetros de linha de comando](#13-parâmetros-de-linha-de-comando)
14. [Como executar](#14-como-executar)
15. [Calibração e desvios da especificação original](#15-calibração-e-desvios-da-especificação-original)
16. [Limitações conhecidas](#16-limitações-conhecidas)
17. [Mapa do código-fonte](#17-mapa-do-código-fonte)

---

## 1. Visão geral

[`nori-cmf.cc`](../examples/nori-cmf.cc) é um exemplo do módulo **NORI** para o ns-3 que reproduz, em nível de sistema, o cenário de rede descrito no artigo *"Conflict Mitigation Framework and Conflict Detection in O-RAN nRT-RIC"* (IEEE ComMag, 2023) — o mesmo cenário sobre o qual o [`xApp-CMF`](../../../xapp-CMF) exercita o Conflict Mitigation Framework (CMF) propriamente dito. O exemplo simula:

- uma rede de **19 estações-base** em grade hexagonal, com **380 usuários** móveis;
- **dois xApps** que competem pela fronteira de handover de cada célula — **MRO** (Mobility Robustness Optimization) e **MLB** (Mobility Load Balancing) — emulados internamente para testes offline, ou controlados por xApps reais via E2;
- uma **interface E2 real** por célula, capaz de reportar KPMs e receber comandos RIC Control de um Near-RT RIC de verdade, com um modo totalmente offline para reprodutibilidade.

**Este arquivo não detecta nem mitiga conflitos.** Ele só aplica, sem arbitrar, qualquer decisão de controle que receber — a detecção e a mitigação são responsabilidade do [`xApp-CMF`](../../../xapp-CMF) ([seção 10](#10-o-conflict-mitigation-framework-cmf)), que roda separadamente no Near-RT RIC.

O objetivo didático deste documento é permitir que qualquer pessoa leia o `.cc` (~1700 linhas) entendendo *por que* cada bloco existe, não apenas *o que* ele faz.

---

## 2. Contexto: o repositório de referência

O código foi escrito a partir do repositório público
[`czezy/O-RAN_CMF_CM2023`](https://github.com/czezy/O-RAN_CMF_CM2023), que documenta o cenário do artigo. É importante entender uma particularidade desse repositório: **ele não contém código-fonte**. Ele traz apenas:

- um `README.md` com a especificação textual do cenário (topologia, parâmetros de rádio, mobilidade, tráfego, tabelas de decisão dos xApps);
- os formatos JSON das mensagens do framework de conflitos (`json_messages/ICD`, `DCD`, `ImCD`);
- os resultados brutos em CSV de três execuções (`simulation_results/no_CM`, `prio_MRO`, `prio_MLB`);
- duas figuras (`figures/base_stations.png`, `figures/users.png`).

Ou seja, `nori-cmf.cc` **não é uma tradução de código C++ existente** — é uma reimplementação escrita a partir da especificação, **calibrada** contra os CSVs publicados sempre que o README deixava um parâmetro em aberto ou internamente inconsistente (ver [seção 15](#15-calibração-e-desvios-da-especificação-original)).

---

## 3. O problema: conflitos entre xApps

Em uma arquitetura O-RAN, múltiplos xApps rodam simultaneamente no Near-RT RIC e podem escrever parâmetros RAN que interagem entre si. O artigo de referência define uma taxonomia de três tipos de conflito, cada um com seu próprio detector:

| Tipo | Detector | Definição | Exemplo no cenário |
|---|---|---|---|
| **Direto** | DCD (Direct Conflict Detection) | Dois xApps escrevem o **mesmo** parâmetro da mesma célula, com valores diferentes. | Dois xApps de MRO distintos escrevendo `TxPower`. |
| **Indireto** | ICD (Indirect Conflict Detection) | Dois xApps escrevem parâmetros **diferentes** que pertencem ao **mesmo grupo funcional** (ex.: parâmetros que juntos definem a fronteira de handover). | MRO escreve `HOHysteresis`/`HOTimeToTrigger` e MLB escreve `HOMeasurementOffset` (CIO) — os três decidem, juntos, quando um handover acontece. |
| **Implícito** | ImCD (Implicit Conflict Detection) | Nenhum parâmetro colide diretamente, mas a ação combinada de dois xApps **degrada um KPI** monitorado. | A soma dos ajustes de MRO e MLB piora a satisfação média dos usuários mesmo que cada xApp, isoladamente, estivesse "correto". |

No cenário do artigo — e portanto neste exemplo — o conflito estrutural é **indireto**: `HOMeasurementOffset` (CIO, escrito pelo MLB), `HOHysteresis` e `HOTimeToTrigger` (escritos pelo MRO) pertencem todos ao grupo de parâmetros `CellAffectHandoverBoundary`, porque juntos controlam a condição de entrada do evento A3 que dispara um handover. Ajustar CIO para balancear carga pode anular o efeito de uma histerese alta pensada para reduzir ping-pongs, e vice-versa.

O CMF pode operar em três modos, correspondentes aos três conjuntos de resultados publicados no repositório de referência:

- **`none`** (`no_CM`): conflitos são detectados e registrados, mas nenhuma decisão é descartada;
- **`prioMRO`** (`prio_MRO`): em caso de conflito, a decisão do MRO prevalece e a do MLB é descartada;
- **`prioMLB`** (`prio_MLB`): o inverso — a decisão do MLB prevalece.

---

## 4. Arquitetura da simulação

`nori-cmf.cc` **não** usa a pilha completa de PHY/MAC do NR (5G-LENA). Essa é uma decisão de projeto deliberada: o cenário do artigo tem 19 células, 380 UEs e 1000 s de simulação — inviável em tempo de execução razoável com PHY/MAC completos, já que o número de UEs e a duração são ordens de grandeza maiores que os exemplos padrão do módulo NR. Em vez disso, o exemplo é uma **simulação de nível de sistema (system-level)**: usa o *scheduler* de eventos, o RNG e o modelo de mobilidade do ns-3, mas substitui PHY/MAC por um modelo analítico de rádio (pathloss 3GPP TR 38.901) e por uma contabilidade explícita de PRBs por célula.

O núcleo é a classe `CmfSimulation` (linha ~453), que concentra todo o estado da simulação:

```cpp
class CmfSimulation
{
  public:
    explicit CmfSimulation(const CmfConfig& cfg);
    void Setup();
    void Run();

  private:
    // construção do cenário, laço principal, laço de controle,
    // relatórios, callbacks E2 ...
    CmfConfig m_cfg;
    std::vector<BaseStation> m_bs;
    std::vector<UserEquipment> m_ue;
    ...
};
```

Três laços de eventos independentes são agendados em `Setup()` e se reagendam a si mesmos a cada iteração:

```cpp
Simulator::Schedule(Seconds(m_cfg.stepTime), &CmfSimulation::Step, this);
Simulator::Schedule(Seconds(m_cfg.kpiPeriod), &CmfSimulation::CollectKpis, this);
Simulator::Schedule(Seconds(m_cfg.controlPeriod), &CmfSimulation::RunXapps, this);
```

| Laço | Período padrão | Responsabilidade |
|---|---|---|
| `Step()` | 0,05 s | Mobilidade, rádio, handover, admissão/liberação de conexões, alocação de PRBs — o "plano de dados" da simulação. |
| `CollectKpis()` | 1 s | Agrega os KPIs de rede (carga, satisfação, balanceamento) e grava os CSVs de saída — o "plano de gerência". |
| `RunXapps()` | 1 s | Roda a lógica dos xApps MRO/MLB, detecta conflitos via CMF e aplica as decisões aceitas — o "plano de controle". |

Essa separação em três períodos independentes é intencional e reflete a arquitetura real: o plano de dados evolui rápido (mobilidade, rádio), enquanto o plano de controle do RIC opera em ciclos mais lentos, tipicamente segundos.

---

## 5. O cenário de rede

### 5.1 Topologia das estações-base

`BuildBaseStations()` (linha ~579) constrói uma grade hexagonal de 19 células, organizadas em cinco fileiras de 3-4-5-4-3 estações, reproduzindo a figura `figures/base_stations.png` do repositório de referência:

```cpp
static const int kRowSize[5] = {3, 4, 5, 4, 3};
```

Cada fileira é espaçada verticalmente por uma distância entre sites (`isd`, 1200 m por padrão) e centralizada horizontalmente; fileiras alternadas ficam deslocadas meio ISD, formando o padrão hexagonal clássico de redes celulares.

### 5.2 Área de cobertura

`BuildBoundary()` (linha ~612) define um hexágono que aproxima a borda de cobertura da rede, também reproduzindo a figura de referência. Esse polígono é usado tanto para distribuir os usuários iniciais quanto para o modelo de mobilidade "quicar na borda".

### 5.3 Distribuição dos usuários

`BuildUsers()` (linha ~631) gera 380 UEs por *rejection sampling* dentro do polígono de cobertura (sorteia um ponto no retângulo delimitador e descarta se cair fora do hexágono). Cada UE recebe:

- uma direção de movimento aleatória e uma velocidade (pedestre ou veicular, [seção 7](#7-mobilidade-e-tráfego));
- um perfil de tráfego, que define a taxa de bits demandada;
- um estado de linha de visada (LOS/NLOS) independente para cada uma das 19 células, sorteado segundo a probabilidade de LOS do modelo UMa ([seção 6](#6-modelo-de-rádio)).

---

## 6. Modelo de rádio

O modelo de propagação implementa o **UMa (Urban Macro)** do 3GPP TR 38.901, exatamente como especificado no README de referência.

### 6.1 Probabilidade de linha de visada

`UmaLosProbability()` (linha ~211) implementa a Tabela 7.4.2-1 do TR 38.901 para altura de UE abaixo de 13 m:

```cpp
double UmaLosProbability(double d2d)
{
    if (d2d <= 18.0) return 1.0;
    return 18.0 / d2d + std::exp(-d2d / 63.0) * (1.0 - 18.0 / d2d);
}
```

Cada UE tem seu estado LOS/NLOS por célula redesenhado sempre que se move mais que a `losCorrelationDistance` (50 m, valor do TR 38.901) desde o último sorteio — isso evita que o estado LOS "pisque" a cada passo de simulação, respeitando a coerência espacial do modelo.

### 6.2 Perda de percurso (pathloss)

`UmaPathloss()` (linha ~222) implementa a Tabela 7.4.1-1 do TR 38.901, com a distância de breakpoint (`dBp`) calculada a partir das alturas efetivas de antena e da frequência portadora, e a fórmula NLOS tomando o máximo entre a perda LOS e a fórmula NLOS específica (como manda a norma).

### 6.3 Potência recebida e margens

`RxPowerDbm()` (linha ~789) soma ao pathloss todas as margens listadas no README de referência — perda corporal, margem de *slow fading*, perda por folhagem e margem de chuva — além dos ganhos/perdas de antena e cabo de BS e UE:

```cpp
return m_cfg.bsTxPowerDbm + m_cfg.bsAntennaGainDb - m_cfg.bsCableLossDb +
       m_cfg.ueAntennaGainDb - m_cfg.ueCableLossDb - pl - m_cfg.bodyLossDb -
       m_cfg.slowFadingMarginDb - m_cfg.foliageLossDb - m_cfg.rainMarginDb;
```

Esse valor é expresso **por PRB** (ver nota de calibração na [seção 15](#15-calibração-e-desvios-da-especificação-original)): é a potência que entraria no cálculo de SINR de um único bloco de recurso, não a potência somada sobre toda a portadora de 20 MHz.

### 6.4 SINR e interferência

`UpdateRadio()` (linha ~829) calcula, para cada par (UE, célula), a SINR incluindo interferência de todas as outras 18 células. A interferência de cada célula vizinha é **ponderada pela carga da célula no passo anterior** (`bs.loadPrevStep`): uma célula ociosa transmite em poucos PRBs e, portanto, interfere pouco; uma célula lotada transmite em todos os 100 PRBs e interfere ao máximo. Essa é uma aproximação de reuso de frequência total ponderado por carga, sem exigir alocação explícita de PRB por PRB entre células.

O ruído térmico é calculado com a fórmula padrão `-174 dBm/Hz + 10·log10(largura de banda) + figura de ruído`, integrado sobre a largura de um único PRB (180 kHz: 12 subportadoras × 15 kHz), de forma consistente com a potência recebida por PRB.

### 6.5 Eficiência espectral e MIMO

`SpectralEfficiency()` (linha ~804) aplica a fórmula de Shannon truncada no teto de eficiência espectral do CQI 15 da tabela 3GPP (5,5547 bits/s/Hz), com suporte a MIMO 2×2: acima de um limiar de SINR configurável (6 dB por padrão), a simulação assume que os dois fluxos espaciais (streams) são usados, dobrando a capacidade; abaixo do limiar, apenas um stream é usado — reflete o comportamento real de seleção de rank em MIMO.

`PrbsNeeded()` (linha ~812) converte uma demanda em bps e uma SINR em número de PRBs necessários, arredondando para cima e sinalizando "não atendível" (`PRB_PER_CELL + 1`) quando a demanda excede a capacidade de uma célula inteira.

---

## 7. Mobilidade e tráfego

### 7.1 Modelo de mobilidade

`UpdateMobility()` (linha ~881) implementa um *random directional model* simples, fiel ao README de referência:

- 80% dos usuários são pedestres (5 m/s), 20% são veiculares (25 m/s);
- a cada passo de 0,05 s, a direção muda aleatoriamente com probabilidade de 0,06%;
- ao atingir a borda do polígono de cobertura, uma nova direção é sorteada até encontrar uma que leve o UE de volta para dentro da área (até 16 tentativas).

### 7.2 Perfis de tráfego

`BuildUsers()` sorteia, para cada UE, um perfil de demanda de bitrate segundo as probabilidades do README:

| Perfil | Taxa demandada | Probabilidade |
|---|---|---|
| Baixa | 96 kbps | 60% |
| Média | 5000 kbps | 30% |
| Alta | 24000 kbps | 10% |

### 7.3 Tentativas de conexão e duração

O processo de tráfego segue um processo de Poisson aproximado por normais truncadas: o intervalo entre tentativas de conexão e a duração de cada conexão são amostrados de `NormalRandomVariable`s configuradas com média/desvio do README (20±3 s entre tentativas, 60±15 s de duração), com um piso de 1 s para evitar valores negativos ou nulos.

---

## 8. Handover A3 e o laço de controle

### 8.1 Ciclo de vida de uma conexão

O laço `Step()` (linha ~1236) processa, nesta ordem, cada passo de 0,05 s:

```cpp
void CmfSimulation::Step()
{
    UpdateMobility();
    UpdateRadio();
    ReleaseExpiredConnections();
    DetectRadioLinkFailures();
    EvaluateHandovers();
    HandleConnectionAttempts();
    AllocateResources();
    // snapshot de carga para a interferência do próximo passo
}
```

A ordem importa: os PRBs são liberados (`ReleaseExpiredConnections`, `DetectRadioLinkFailures`) **antes** de novas tentativas de conexão serem avaliadas (`HandleConnectionAttempts`), para que a capacidade recém-liberada no mesmo passo já esteja disponível.

### 8.2 Evento A3 (handover)

`EvaluateHandovers()` (linha ~1041) implementa o evento **A3** do 3GPP (a base do algoritmo de handover em LTE/NR): um handover é disparado quando a qualidade de uma célula vizinha supera a qualidade da célula servidora por mais que a histerese configurada, **durante** um intervalo de Time-To-Trigger (TTT) contínuo.

```cpp
const double servingQuality = m_rxPowerDbm[base + ue.servingBs] + serving.cio;
...
const double q = m_rxPowerDbm[base + b] + m_bs[b].cio;
...
const bool a3 = bestQuality > servingQuality + serving.hysteresis;
```

Note que o **CIO** (Cell Individual Offset) entra somado à potência recebida de cada célula — é exatamente esse deslocamento que o MLB manipula para "atrair" ou "repelir" handovers de/para uma célula, sem alterar a potência de transmissão real.

O estado `ue.a3Target`/`ue.a3Since` implementa o temporizador do TTT: quando uma nova candidata passa a satisfazer a condição de entrada, o temporizador reinicia; só quando a mesma candidata permanece melhor por `serving.ttt` segundos contínuos é que o handover é de fato executado — e mesmo assim, apenas se a célula-alvo tiver PRBs livres suficientes (`RecordHandover()`, linha ~996).

### 8.3 Falha de enlace de rádio (RLF)

`DetectRadioLinkFailures()` (linha ~943) modela uma falha de enlace como um "temporizador Qout": quando a SINR cai abaixo de `rlfSinrThresholdDb` (ou a potência recebida abaixo da sensibilidade do receptor) por mais que `rlfTimer` segundos contínuos, a conexão é derrubada e os PRBs são liberados.

### 8.4 Ping-pong

Um handover é classificado como *ping-pong* em `RecordHandover()` quando o UE retorna à célula de origem de um handover anterior dentro do período configurado (`pingPongPeriod`, 10 s por padrão) — exatamente a definição do README de referência. Handovers e ping-pongs são atribuídos à **célula de origem**, porque é a histerese e o TTT *dessa* célula que governaram a decisão — e são justamente esses os parâmetros que o MRO ajusta.

### 8.5 Admissão de novas conexões

`HandleConnectionAttempts()` (linha ~1109) ordena as 19 células por potência recebida (já incluindo o CIO — o MLB também influencia *onde* uma conexão nova é aceita, não só os handovers) e tenta admitir o UE nas três candidatas mais fortes, na ordem. Se nenhuma tiver PRBs livres suficientes, ou a SINR/potência estiver abaixo dos limiares mínimos, a tentativa é registrada como **bloqueio de chamada** (*call blockade*).

### 8.6 Alocação de PRBs

`AllocateResources()` (linha ~1180) recalcula a demanda de PRB de cada UE conectado com a SINR corrente e, quando a soma das demandas de uma célula excede seus 100 PRBs, faz um *downscaling* proporcional (fair-share) entre todos os UEs daquela célula — o que define a satisfação individual de cada UE (razão entre PRBs alocados e PRBs necessários, limitada a 1,0).

---

## 9. Os xApps MRO e MLB

`RunXapps()` (linha ~1262) roda a cada `controlPeriod` (1 s por padrão) e implementa a lógica de **dois** xApps, cada um monitorando uma janela deslizante de eventos por célula (`mroWindow`, 240 s):

```cpp
PruneWindow(bs.hoWindow, now);
PruneWindow(bs.ppWindow, now);
PruneWindow(bs.rlfWindow, now);

const double ppRatio  = bs.ppWindow.size()  / hoCount;
const double rlfRatio = bs.rlfWindow.size() / hoCount;
```

### 9.1 MRO — Mobility Robustness Optimization

O MRO escreve **dois** parâmetros por célula, cada um a partir de uma tabela de degraus do artigo de referência:

- **Time-To-Trigger**, a partir da razão de ping-pong (`PingPongRatioToTtt()`, linha ~273): quanto mais handovers desnecessários (ping-pongs), maior o TTT — a célula passa a exigir uma vantagem sustentada por mais tempo antes de aceitar um handover, reduzindo indecisão.
- **Histerese**, a partir da razão de RLF (`RlfRatioToHysteresis()`, linha ~297): quanto mais falhas de enlace, maior a histerese — a intenção é o oposto: um handover mais **fácil** de disparar (menos margem exigida) reduz o tempo em más condições de rádio. *(A relação exata segue a tabela publicada; ver a nota de calibração na seção 15 sobre os degraus extras adicionados nas extremidades das tabelas.)*

### 9.2 MLB — Mobility Load Balancing

O MLB escreve um único parâmetro: o **CIO**, a partir da carga atual da célula (`LoadToCio()`, linha ~306). Células mais carregadas recebem um CIO maior, o que — combinado à fórmula do evento A3 ([seção 8.2](#82-evento-a3-handover)) — torna a célula menos atraente para handovers de entrada e admissões novas, empurrando tráfego para vizinhas menos carregadas.

### 9.3 Por que MRO e MLB colidem

Observe que **CIO**, **histerese** e **TTT** entram todos na mesma inequação do evento A3:

```
bestQuality (com CIO da vizinha) > servingQuality (com CIO da própria célula) + histerese
                                     [avaliado continuamente por TTT segundos]
```

Um aumento de CIO decidido pelo MLB para aliviar uma célula lotada pode ser parcialmente ou totalmente anulado por um aumento de histerese decidido pelo MRO na mesma célula, pensado para reduzir ping-pongs — e vice-versa. É exatamente esse acoplamento que o [`xapp-CMF`](../../../xapp-CMF) formaliza como um único grupo `CellAffectHandoverBoundary` (`PARAMETER_GROUPS` em `xapp-CMF/src/cd_agent.py`), usado pelo detector ICD.

---

## 10. O Conflict Mitigation Framework (CMF)

> **O CMF não vive mais neste arquivo.** Versões anteriores deste exemplo embutiam a detecção e mitigação de conflitos diretamente na simulação (`DetectAndMitigate()`, `ReportConflict()`, o parâmetro `--cmMode`). Isso foi removido: `nori-cmf.cc` hoje é *só* o cenário de RAN + nó E2 — ele aplica, sem arbitrar, qualquer decisão de controle que receber, seja da emulação interna de MRO/MLB (modo offline), seja de um xApp real via E2.
>
> O CMF (CD Agent, CR Agent, PMon) agora é um xApp separado, [`xapp-CMF`](../../../xapp-CMF), rodando no Near-RT RIC — exatamente onde o artigo de referência ([Adamczyk & Kliks, IEEE ComMag 2023](https://arxiv.org/abs/2305.07117)) o posiciona (Fig. 2 do artigo: o CM é uma entidade do Near-RT RIC, não do simulador de RAN). Os xApps MRO e MLB submetem cada decisão ao `xapp-CMF` (`POST /ric/v1/cmf/evaluate`) *antes* de enviar um RIC Control Request; se rejeitada, a decisão simplesmente não é enviada.
>
> Para a implementação de DCD, ICD, ImCD e do CR Agent, veja [`xapp-CMF/README.md`](../../../xapp-CMF/README.md) e os módulos `src/cd_agent.py`, `src/cr_agent.py`, `src/pmon.py` daquele xApp. A [seção 15.3](#153-validação-numérica) abaixo preserva a validação numérica feita quando o CMF ainda era embutido — os números continuam válidos como evidência de que o modelo de rádio/mobilidade e as tabelas de decisão de MRO/MLB estão corretos, só não refletem mais o caminho de mitigação (que agora é testado a partir do `xapp-CMF`).

---

## 11. Integração com o Near-RT RIC via E2

### 11.1 Uma célula, um nó E2

`SetupE2Terminations()` (linha ~734) cria uma instância de `E2Termination` **por célula** (19 no total), cada uma conectando de forma independente ao endereço do RIC (`--ipE2TermRic`), registrando dois modelos de serviço:

- **KPM** (RAN Function ID 200): reporta indicações periódicas de KPI;
- **RIC Control** (RAN Function ID 300): recebe comandos de controle de xApps reais no RIC.

```cpp
bs.e2Term->RegisterKpmCallbackToE2Sm(RAN_FUNCTION_KPM, kpmFd, ...KpmSubscriptionCallback...);
bs.e2Term->RegisterSmCallbackToE2Sm(RAN_FUNCTION_RC, ricFd, ...RicControlCallback...);
```

Esse desenho segue o mesmo padrão dos demais exemplos do módulo NORI (`nori-sample.cc`, `nori-mimo-demo.cc`), mas aplicado 19 vezes — uma por gNB simulado.

### 11.2 Indicações KPM

Quando o RIC confirma uma assinatura (`KpmSubscriptionCallback()`, linha ~1460), a célula passa a enviar indicações periódicas (`SendKpmIndication()`, linha ~1470) contendo:

- o contêiner O-DU padrão do E2SM-KPM (PRBs disponíveis/usados, QCI);
- uma lista de métricas customizadas consumidas pelos xApps MRO/MLB: `HO.TotNbrOut`, `HO.PingPongNbrOut`, `RRC.ReEstabAtt.RLF`, `RRU.PrbUsedDl`, `DRB.MeanActiveUeDl`;
- os **valores correntes** dos três parâmetros disputados — `MLB.CioMilliDb`, `MRO.HysteresisMilliDb`, `MRO.TimeToTriggerMs` — codificados em milésimos de unidade porque o encoder ASN.1 do E2SM-KPM só aceita inteiros.

### 11.3 Comandos RIC Control

`RicControlCallback()` (linha ~1571) decodifica uma mensagem E2SM-RC Control recebida do RIC. A convenção usada (documentada no próprio código) é uma lista de pares `[id do parâmetro, valor em milésimos]`:

| id | Parâmetro | Origem esperada |
|---|---|---|
| 1 | `HOMeasurementOffset` (CIO) | xApp MLB real |
| 2 | `HOHysteresis` | xApp MRO real |
| 3 | `HOTimeToTrigger` | xApp MRO real |

### 11.4 Convivência entre xApp real e emulação interna

Como não existe (neste ambiente) um xApp MRO/MLB publicado rodando no RIC, o exemplo mantém uma **emulação interna** desses dois xApps ([seção 9](#9-os-xapps-mro-e-mlb)), que roda sempre — inclusive com `--useE2=1`. Quando uma decisão chega via E2 (`ApplyRicDecision()`, linha ~1623), a célula marca aquele parâmetro específico como "propriedade" de um xApp real:

```cpp
bs.ricOwned.insert(d.parameter);
```

e `RunXapps()` para de gerar decisões da emulação interna para aquele parâmetro naquela célula (`if (bs.ricOwned.count(RanParameter::Cio) == 0) { ... }`). Isso permite:

- rodar o cenário **totalmente offline** (`--useE2=0`), sem depender de infraestrutura externa — sem `xapp-CMF`, nada mitiga conflitos nesse modo, é equivalente ao baseline "CM disabled" do artigo; é assim que os CSVs de validação deste documento foram gerados;
- rodar com **um RIC real** e xApps MRO/MLB reais controlando algumas ou todas as células, sem que a emulação interna "brigue" com o xApp de verdade pelo mesmo parâmetro;
- misturar os dois: por exemplo, um xApp MRO real no RIC e a emulação interna cobrindo o MLB, testando a integração incremental de um xApp por vez.

Toda decisão de um xApp real, decodificada aqui, é aplicada diretamente — a arbitragem entre decisões conflitantes já aconteceu antes, no [`xapp-CMF`](../../../xapp-CMF), que os xApps MRO/MLB consultam antes de sequer enviar o RIC Control Request.

> **Nota de verificação**: o laço fim-a-fim contra um Near-RT RIC real com os xApps MRO e MLB deste projeto foi exercitado e validado em sessões posteriores (ver os READMEs de [`xapp-MRO`](../../../xapp-MRO/README.md) e [`xapp-MLB`](../../../xapp-MLB/README.md)); o caminho offline (`--useE2=0`) foi validado numericamente contra os CSVs de referência (seção 15).

---

## 12. Arquivos de saída

`OpenTraceFiles()` (linha ~696) cria, em `--outputDir`, o mesmo conjunto de arquivos do repositório de referência, com o mesmo esquema de colunas — o que permite reaproveitar diretamente qualquer script de análise já escrito para os CSVs originais.

### 12.1 CSVs (um valor por linha de tempo, exceto os eventos)

| Arquivo | Colunas | Granularidade | Conteúdo |
|---|---|---|---|
| `avail.csv` | `time, availability` | 1 s | Disponibilidade média (1 − carga) das 19 células. |
| `satis.csv` | `time, satisfaction` | 1 s | Satisfação média dos UEs conectados (PRBs alocados / necessários). |
| `lb.csv` | `time, lb ratio` | 1 s | Índice de Jain sobre a carga das 19 células (`JainFairness()`, linha ~353). |
| `cb.csv` | `time, user, x pos, y pos` | por evento | Um registro por bloqueio de chamada. |
| `rlf.csv` | `time, current bs, user, conn_sinr, x pos, y pos` | por evento | Um registro por falha de enlace de rádio. |
| `ho.csv` | `time, previous bs, current bs, user, conn_sinr, x pos, y pos` | por evento | Um registro por handover. |
| `pp.csv` | `time, current bs, user, conn_sinr, x pos, y pos, ho pp time` | por evento | Um registro por handover classificado como ping-pong. |
| `bs-<id>.csv` | `time, current bs, availability, cio, hyst, ttt` | 1 s | Série temporal por célula dos três parâmetros disputados. |
| `summary.txt` | — | fim da execução | Resumo agregado (ver `WriteSummary()`, linha ~1426), também impresso no terminal. |

### 12.2 Log de conflitos

Não existe mais aqui: `nori-cmf.cc` não detecta conflitos, então não há `conflicts.json` neste diretório de saída. O log equivalente (`/tmp/conflicts.json`, formato *JSON Lines* no mesmo esquema `json_messages/{ICD,DCD,ImCD}/*signal conflict.json` do repositório de referência) agora é escrito pelo [`xapp-CMF`](../../../xapp-CMF), dentro do seu próprio pod — veja `_log_conflict()`/`_on_implicit_conflict()` em `xapp-CMF/src/custom_xapp.py`.

---

## 13. Parâmetros de linha de comando

Todos os parâmetros abaixo são definidos em `CmfConfig` (linha ~111) e expostos via `CommandLine` em `main()` (linha ~1655).

| Flag | Padrão | Descrição |
|---|---|---|
| `--simTime` | 1000 | Tempo total simulado, em segundos. |
| `--stepTime` | 0.05 | Período de atualização de posição/rádio, em segundos. |
| `--kpiPeriod` | 1 | Período de coleta de KPIs, em segundos. |
| `--controlPeriod` | 1 | Período do laço de controle dos xApps, em segundos. |
| `--warmupTime` | 150 | Tempo inicial descartado das médias finais. |
| `--nUe` | 380 | Número de usuários equipamentos. |
| `--isd` | 1200 | Distância entre estações-base, em metros. |
| `--rxSensitivity` | -120 | Sensibilidade do receptor do UE, em dBm (por PRB). |
| `--rlfSinrThreshold` | -18 | SINR abaixo da qual o enlace é considerado em falha, em dB. |
| `--pingPongPeriod` | 10 | Janela de detecção de ping-pong, em segundos. |
| `--mroWindow` | 240 | Janela estatística do xApp MRO, em segundos. |
| `--outputDir` | `cmf-output` | Diretório de saída dos CSVs. |
| `--rngSeed` | 1 | Semente do gerador de números aleatórios. |
| `--rngRun` | 1 | Número de execução (RNG run) do ns-3. |
| `--useE2` | `true` | Conecta cada célula ao Near-RT RIC via E2. |
| `--ipE2TermRic` | `10.244.0.108` | Endereço IP do termination E2 do RIC. |
| `--e2Periodicity` | 1 | Período das indicações KPM, em segundos. |

Parâmetros adicionais existem em `CmfConfig` mas não são expostos por linha de comando (por exemplo, os parâmetros de rádio do link budget, as probabilidades de perfil de tráfego, ou os limiares do CMF) — para alterá-los é necessário editar os valores padrão da struct diretamente no código.

---

## 14. Como executar

O exemplo é registrado em [`examples/CMakeLists.txt`](../examples/CMakeLists.txt) e compilado junto com o restante do módulo:

```bash
cd ~/ns-3-dev
./ns3 build nori-cmf
```

### 14.1 Execução offline (recomendada para reprodução dos resultados)

```bash
./ns3 run "nori-cmf -- --useE2=0 --simTime=1000 --outputDir=/tmp/cmf-none"
```

Sem `xapp-CMF` (que só existe no caminho E2), este modo sempre se comporta como o baseline "CM disabled" do artigo — nada mitiga os conflitos entre a emulação interna de MRO e MLB.

Ou, usando o script auxiliar [`run_nori_cmf.sh`](../../../run_nori_cmf.sh) na raiz do repositório ns-3:

```bash
./run_nori_cmf.sh --sim-time 200 --warmup-time 50 --output-dir /tmp/teste
```

### 14.2 Execução conectada a um Near-RT RIC real

```bash
./run_nori_cmf.sh --use-e2
```

Esse modo localiza o IP do pod `deployment-ricplt-e2term-alpha` via `kubectl` (mesmo padrão dos demais scripts `run_nori_*.sh`) e roda com `--useE2=1`. Como a comunicação E2 é em tempo real, a simulação passa a usar o `RealtimeSimulatorImpl` do ns-3 e o tempo de execução passa a ser aproximadamente igual ao `--simTime` configurado (em vez de ser limitado por CPU).

### 14.3 Tempo de execução

Em modo offline, 1000 s simulados levam cerca de **3 minutos** de CPU (medido em ambiente de desenvolvimento single-thread). Para testes rápidos durante o desenvolvimento, reduza `--simTime` e `--warmupTime` proporcionalmente.

---

## 15. Calibração e desvios da especificação original

Como descrito na [seção 2](#2-contexto-o-repositório-de-referência), vários parâmetros do README de referência ou são omitidos, ou são internamente inconsistentes com o restante da especificação. Cada um desses pontos está documentado como comentário junto à constante correspondente no código; esta seção consolida os mais importantes.

### 15.1 Parâmetros inferidos a partir dos CSVs publicados

| Parâmetro | Como foi descoberto | Valor usado |
|---|---|---|
| PRBs por célula | O traço `availability` de cada BS assume exatamente 101 valores distintos em [0, 1] — evidência direta de uma grade de 100 PRBs. | `PRB_PER_CELL = 100` |
| `lb ratio` | O valor mínimo observado nos CSVs (0,0526…) é exatamente 1/19 — a assinatura do índice de Jain aplicado a 19 amostras quando só uma célula está ocupada. | Índice de Jain (`JainFairness()`) |
| Distância entre sites (ISD) | O README indica 600 m, mas a figura `base_stations.png` e a extensão das coordenadas `x pos`/`y pos` nos CSVs de eventos (handover, RLF) são consistentes apenas com ~1200 m. | `isd = 1200` m |
| Degraus extra nas tabelas do MRO/MLB | As tabelas do README cobrem até "razão = 100%", mas os CSVs `bs-N` contêm valores de CIO (3,5 dB), histerese (10 dB) e TTT (5,12 s) que **não aparecem** em nenhuma linha das tabelas publicadas — são o degrau seguinte, implícito quando a razão satura em exatamente 100%. | Um degrau extra adicionado ao final de cada tabela (`PingPongRatioToTtt()`, `RlfRatioToHysteresis()`, `LoadToCio()`). |

### 15.2 Parâmetros corrigidos por inconsistência física

O README especifica 28 dBm de potência de transmissão e -80 dBm de sensibilidade do receptor. Tomados literalmente sobre uma portadora de 20 MHz, esses dois valores tornam **toda** a rede inoperante: o ruído térmico sozinho, integrado sobre 20 MHz com 7 dB de figura de ruído, já fica acima de -80 dBm, e a potência recebida na borda de célula (com todas as margens do README aplicadas) fica dezenas de dB abaixo de qualquer sensibilidade razoável. Isso foi confirmado empiricamente durante o desenvolvimento: a primeira versão do modelo, usando os valores literais, produzia **centenas de RLFs e quase nenhum handover bem-sucedido** em uma simulação de teste — sintoma de uma rede inteira fora de cobertura.

A correção adotada, documentada nos comentários de `CmfConfig`:

- **`bsTxPowerDbm = 28`** passou a ser interpretado como potência **por PRB** (equivalente a 48 dBm somados sobre os 20 MHz), e não como potência total da portadora — essa é a leitura que faz o link budget fechar em uma macro célula UMa a 2,1 GHz.
- **`rxSensitivityDbm`** foi ajustada de -80 para **-120 dBm por PRB**, a sensibilidade de referência de um receptor macro a 2,1 GHz sobre a largura de um PRB.
- **`rlfSinrThresholdDb`** foi calibrada em **-18 dB**, valor que reproduz a mediana de SINR observada nos eventos de RLF dos CSVs de referência (-17,4 dB).

Todos os três permanecem configuráveis por linha de comando (`--rxSensitivity`, `--rlfSinrThreshold`; a potência de transmissão exige editar o padrão em `CmfConfig`, pois não foi exposta via `CommandLine`).

### 15.3 Validação numérica

Rodando os três modos por 1000 s (150 s de aquecimento descartados), os principais KPIs agregados ficam próximos dos valores publicados:

| KPI | Referência (`no_CM`) | Esta implementação (`none`) |
|---|---|---|
| Carga média das BS | 0,83 | 0,78 |
| Satisfação média do usuário | 0,94 | 0,947 |
| Balanceamento de carga (Jain) | ~0,90 | 0,90 |
| Falhas de enlace de rádio (RLF) | 334 | 290 |
| Bloqueios de chamada | 3278 | 2586 |

Handovers e ping-pongs ficam consistentemente abaixo dos valores de referência (a rede original é bem mais instável em mobilidade — 82% dos handovers do CSV de referência são ping-pong, contra ~22% nesta implementação). Sem acesso ao código-fonte original, essa diferença não pôde ser eliminada sem correr o risco de superajustar o modelo aos números publicados em vez de à especificação — o comportamento qualitativo (mais handovers e mais ping-pongs sem CMF, e uma fração significativa de decisões descartada nos modos `prioMRO`/`prioMLB`) está presente e é consistente com o esperado.

---

## 16. Limitações conhecidas

- **Sem PHY/MAC real**: o modelo de rádio é analítico (pathloss + SINR agregada), não a pilha 5G-LENA. Isso é necessário para viabilizar 19 células × 380 UEs × 1000 s em tempo de execução prático, mas significa que efeitos de escalonamento, HARQ, adaptação de link por subquadro etc. não são modelados.
- **Convenção proprietária no payload do RIC Control**: como não existe uma definição pública de payload E2SM-RC para os parâmetros de mobilidade robustez/balanceamento de carga neste contexto, `RicControlCallback()` usa uma convenção própria (par `[id, valor em milésimos]`). Um xApp MRO/MLB real precisaria falar essa mesma convenção para interoperar — ela está documentada no código-fonte, próximo ao ponto de decodificação.
- **Caminho E2 fim-a-fim não testado contra um RIC real**: ver a nota na [seção 11.4](#114-convivência-entre-xapp-real-e-emulação-interna).
- **Divergência quantitativa em handovers/ping-pongs**: ver [seção 15.3](#153-validação-numérica).

---

## 17. Mapa do código-fonte

Referência rápida de onde encontrar cada assunto dentro de [`nori-cmf.cc`](../examples/nori-cmf.cc):

| Assunto | Função / struct | Linha aprox. |
|---|---|---|
| Constantes do cenário | `N_BS`, `PRB_PER_CELL`, ... | 87 |
| Configuração via linha de comando | `struct CmfConfig` | 111 |
| Pathloss / LOS UMa 38.901 | `UmaLosProbability`, `UmaPathloss` | 211, 222 |
| Tabelas de decisão MRO/MLB (emulação interna) | `PingPongRatioToTtt`, `RlfRatioToHysteresis`, `LoadToCio` | 273–306 |
| Decisão de controle | `struct ControlDecision` | 320 |
| Índice de Jain | `JainFairness` | 353 |
| Estado da célula | `struct BaseStation` | 373 |
| Estado do usuário | `struct UserEquipment` | 419 |
| Construção do cenário | `BuildBaseStations`, `BuildBoundary`, `BuildUsers` | 579, 612, 631 |
| Potência recebida / SINR | `RxPowerDbm`, `UpdateRadio` | 789, 829 |
| Mobilidade | `UpdateMobility` | 881 |
| Liberação / RLF | `ReleaseExpiredConnections`, `DetectRadioLinkFailures` | 921, 943 |
| Handover A3 | `EvaluateHandovers`, `RecordHandover` | 1041, 996 |
| Admissão / bloqueio | `HandleConnectionAttempts` | 1109 |
| Alocação de PRBs | `AllocateResources` | 1180 |
| Emulação interna de xApps (aplica direto, sem arbitragem) | `RunXapps`, `ApplyDecision` | 1262, 1317 |
| Coleta de KPIs | `CollectKpis` | 1353 |
| Resumo final | `WriteSummary` | 1426 |
| Setup E2 por célula | `SetupE2Terminations` | 734 |
| Indicação KPM | `KpmSubscriptionCallback`, `SendKpmIndication` | 1460, 1470 |
| Comando RIC Control (aplica direto, sem arbitragem) | `RicControlCallback`, `ApplyRicDecision` | 1571, 1623 |
| Ponto de entrada | `main` | 1655 |

O CD Agent, o CR Agent e o PMon (DCD/ICD/ImCD) não estão mais neste arquivo — veja o mapa de código de [`xapp-CMF`](../../../xapp-CMF) (`src/cd_agent.py`, `src/cr_agent.py`, `src/pmon.py`, `src/custom_xapp.py`).

---

## Referências

- Repositório de especificação: [`czezy/O-RAN_CMF_CM2023`](https://github.com/czezy/O-RAN_CMF_CM2023)
- 3GPP TR 38.901 — *Study on channel model for frequencies from 0.5 to 100 GHz* (modelo de pathloss UMa)
- 3GPP TS 36.331 / TS 38.331 — evento de medição A3 e parâmetros de handover (CIO, histerese, TTT)
- Módulo NORI: [`README.md`](../README.md) do módulo, demais exemplos em [`examples/`](../examples/)
