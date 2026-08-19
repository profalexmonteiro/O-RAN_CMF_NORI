# xapp-MRO

O xApp-MRO é um xApp OSC que implementa a **Mobility Robustness Optimization (MRO)** para os gNBs simulados pelo módulo NORI do NS-3, em particular o cenário [`nori-cmf.cc`](../ns-3-dev/contrib/nori/examples/nori-cmf.cc) (veja [`contrib/nori/docs/nori-cmf.md`](../ns-3-dev/contrib/nori/docs/nori-cmf.md) para a descrição completa do cenário).

Ele é construído sobre a mesma base do [`xapp-nori`](../xapp-nori), removendo a lógica de RL/network slicing e substituindo-a por um controlador MRO em malha fechada.

> Implante o [`xapp-CMF`](../xapp-CMF) junto com este xApp e o [`xapp-MLB`](../xapp-MLB) se quiser que os conflitos entre os dois sejam de fato mitigados, e não apenas detectados/registrados depois do fato — veja [Conflitos com o MLB, e o CMF](#conflitos-com-o-mlb-e-o-cmf) abaixo.

## O que ele faz

O xApp se inscreve na RAN Function ID 200 (KPM) em todo nó E2 registrado. Cada RIC Indication recebida de uma célula traz, entre outras medições:

- `HO.TotNbrOut` — handovers originados por esta célula na última janela de estatísticas;
- `HO.PingPongNbrOut` — desses, quantos foram ping-pongs;
- `RRC.ReEstabAtt.RLF` — quantas falhas de enlace de rádio (RLF) ocorreram nesta célula na mesma janela;
- `MRO.HysteresisMilliDb` / `MRO.TimeToTriggerMs` — o Hysteresis e o Time-To-Trigger de handover **atualmente ativos** na célula;
- `MLB.CioMilliDb` — o Cell Individual Offset atualmente ativo (aqui apenas leitura — esse parâmetro pertence ao xApp MLB).

A cada indicação, o xApp:

1. Calcula a taxa de ping-pong (`HO.PingPongNbrOut / HO.TotNbrOut`) e a taxa de RLF (`RRC.ReEstabAtt.RLF / HO.TotNbrOut`).
2. Mapeia cada taxa para um valor alvo usando as mesmas tabelas de degraus publicadas em [`czezy/O-RAN_CMF_CM2023`](https://github.com/czezy/O-RAN_CMF_CM2023) e reproduzidas no `nori-cmf.cc`:
   - taxa de ping-pong → Time-To-Trigger (menos ping-pongs exigem menos paciência; mais ping-pongs exigem um TTT maior, para que um handover só dispare diante de uma vantagem sustentada);
   - taxa de RLF → Hysteresis (mais falhas de enlace de rádio exigem uma margem *menor*, para que um handover possa disparar mais cedo, antes que o enlace se degrade ainda mais).
3. Compara o resultado com os valores atualmente ativos na célula (também lidos da mesma indicação).
4. Se algum diferir, envia de volta uma RIC Control Request para essa mesma célula — e somente essa célula — carregando o(s) novo(s) valor(es).

O xApp é puramente reativo e sem estado entre indicações: toda decisão é recalculada do zero a partir do que a célula reporta *neste exato momento*. Isso significa que ele se autocorrige automaticamente caso uma decisão anterior tenha sido sobrescrita ou descartada em outro ponto da malha (veja "Conflitos com o MLB" abaixo) — ele simplesmente continuará propondo o mesmo valor a cada período até que seja aceito.

**O xApp nunca escreve `HOMeasurementOffset` (CIO)** — esse parâmetro é responsabilidade de um xApp MLB (Mobility Load Balancing) separado. Executar os dois sobre as mesmas células é o que exercita o Conflict Mitigation Framework descrito a seguir.

## Conflitos com o MLB, e o CMF

`HOMeasurementOffset` (CIO), `HOHysteresis` e `HOTimeToTrigger` decidem conjuntamente quando um handover é disparado (todos entram na mesma inequação do evento A3). Se um xApp MLB também estiver ajustando o CIO nas mesmas células em que este xApp ajusta Hysteresis/TTT, os dois estão estruturalmente em **conflito indireto**, conforme a taxonomia do [`czezy/O-RAN_CMF_CM2023`](https://github.com/czezy/O-RAN_CMF_CM2023): parâmetros diferentes, mesmo grupo funcional (`CellAffectHandoverBoundary`).

A detecção e a resolução de conflitos **não** são feitas por este xApp, nem pelo `nori-cmf.cc`: são responsabilidade de um terceiro xApp, o [`xapp-CMF`](../xapp-CMF), que implementa os agentes de Conflict Detection (DCD/ICD/ImCD) e de Conflict Resolution de [Adamczyk & Kliks, IEEE ComMag 2023](https://arxiv.org/abs/2305.07117). Antes de enviar uma RIC Control Request para um novo valor de Hysteresis/TTT, este xApp primeiro submete a proposta ao `POST /ric/v1/cmf/evaluate` do `xapp-CMF` e só prossegue se a resposta for `{"allowed": true}` — veja o [README do `xapp-CMF`](../xapp-CMF/README.md) para o protocolo completo e os modos de resolução (`none`, `prioMRO`, `prioMLB`). Se o `xapp-CMF` estiver inacessível, essa chamada falha aberta (*fails open*) (um aviso é registrado em log, e a decisão prossegue sem mitigação), em vez de travar o controle da RAN por causa de uma dependência ausente.

Todo conflito detectado é registrado pelo `xapp-CMF` em `/tmp/conflicts.json` dentro do seu próprio pod, no mesmo formato JSON documentado no repositório de referência.

## Formato de mensagem (wire format)

Não existe um payload E2SM-RC padronizado para parâmetros de limite de handover, então este xApp usa a mesma convenção que o `RicControlCallback()` do `nori-cmf.cc` decodifica:

- A RIC Control Request tem como alvo a **RAN Function ID 300** (RIC Control), a function ID que o `nori-cmf.cc` registra para mensagens de controle.
- A mensagem de controle é um `E2SM-RC-ControlMessage-Format1` padrão, cuja `ranParameters-List` carrega um `RANParameter-Item` por parâmetro alterado:

  | `ranParameterItem-ID` | Parâmetro | Escrito por |
  |---|---|---|
  | 1 | `HOMeasurementOffset` (CIO) | xApp MLB (não este) |
  | 2 | `HOHysteresis` | **este xApp** |
  | 3 | `HOTimeToTrigger` | **este xApp** |

- O valor de cada item é um `INTEGER` (`valueInt`) em **mili-unidades** — milidecibéis para CIO/Hysteresis, milissegundos para TTT — já que o tipo ASN.1 subjacente não tem representação decimal nativa.
- O RIC Control Header (`E2SM-RC-ControlHeader-Format1`) é preenchido com valores de `ueId`/estilo/ação de preenchimento (esse controle é por célula, não por UE) e omite o campo opcional `rrmPolicyList`, específico do controle de RAN slicing e sem relação com o MRO.

Veja `build_ric_control_pdu()` em [`src/custom_xapp.py`](src/custom_xapp.py) para a codificação exata.

> **Nota sobre o módulo nori subjacente**: fazer esse caminho de controle funcionar de ponta a ponta exigiu três pequenas correções em `contrib/nori/model/{asn1c-types,ric-control-message}.cc`, no módulo `nori` do ns-3 — o decodificador genérico de `ranParameters-List` estava anteriormente stubado para sempre retornar uma lista vazia, `RANParameterItem` tinha um bug de double-free ao ser copiado (por consequência, o ID do parâmetro ficava inacessível fora da classe), e o campo opcional `rrmPolicyList` do cabeçalho de controle era desreferenciado incondicionalmente. As três correções estão presentes no módulo `nori` contra o qual este xApp foi desenvolvido; se você observar mensagens de controle decodificadas retornando vazias, ou um crash ao chegar uma requisição de controle sem políticas de slicing, verifique se o seu checkout do `nori` inclui essas correções.

## Requisitos

Todos os comandos assumem que:

- Você está executando uma VM [OpenRAN@Brasil Blueprint v1](https://github.com/LABORA-INF-UFG/openran-br-blueprint/wiki/OpenRAN@Brasil-Blueprint-v1), com `kubectl`, `docker` e `dms_cli` disponíveis e um registro local de imagens acessível em `127.0.0.1:5001`
- Uma plataforma Near-RT RIC (namespace `ricplt`) está implantada e saudável
- Você está dentro da pasta do repositório `xapp-MRO/`

## Embarcando no Near-RT RIC, passo a passo

Este passo a passo cobre tudo, desde "o RIC está no ar" até "ver a malha de controle do MRO de fato mover o Hysteresis/TTT de uma célula". Foi escrito a partir de, e verificado contra, uma sessão real de onboarding em um cluster ativo — incluindo as arestas mais ásperas.

### 1. Confirme que a plataforma RIC está saudável

```bash
kubectl get pods -n ricplt
```

Todo pod deve mostrar `1/1` ou `2/2` em `READY`. Se não, veja [Solução de problemas da plataforma](#solução-de-problemas-da-plataforma) abaixo antes de continuar — embarcar um xApp contra um RIC parcialmente iniciado até funciona, mas as inscrições (subscriptions) vão falhar.

### 2. Compile, publique e instale o xApp

```bash
bash update_xapp.sh
```

Este único script faz tudo: (a) embarca o chart do xApp (`dms_cli onboard init/config-file.json init/schema.json`), (b) remove qualquer instalação/imagem anterior do `xappmro`, (c) faz o `docker build` da imagem e a publica em `127.0.0.1:5001/xappmro:1.0.0`, e (d) faz o `dms_cli install` no namespace `ricxapp`. Um primeiro build baixa e compila o `rmr`, clona o `ric-plt-xapp-frame-py` e instala as dependências Python — espere que leve vários minutos na primeira vez; execuções posteriores reaproveitam o cache de camadas do Docker e são bem mais rápidas (o código do xApp em si é só as últimas camadas).

O script aguarda o pod atingir `1/1` sozinho e exibe (tail) suas primeiras linhas de log. Se em vez disso você ver `CrashLoopBackOff`, vá direto para [Solução de problemas do xApp](#solução-de-problemas-do-xapp).

### 3. Confirme que o pod está em execução

```bash
kubectl get pods -n ricxapp
```

Você deve ver `ricxapp-xappmro-...` em `1/1 Running`. Nesse ponto, o xApp já tentou, uma vez, se inscrever em todo nó E2 que o RIC conhecia até então (`RANFunctionID: 200`, ou seja, KPM). Verifique o que aconteceu:

```bash
bash log_xapp.sh
```

Procure linhas como `Subscription response from <gNB>: status = 201, reason = Created` (sucesso) em contraste com `Failed to subscribe to node <gNB>. Status code: 503` (o nó E2 não está de fato acessível, ou o `submgr` ainda não estava pronto — muito comum logo após o RIC ou o xApp terem acabado de subir, veja abaixo).

### 4. Inicie (ou confirme) a conexão do `nori-cmf`

Se ainda não estiver em execução, descubra o IP do pod `e2term` e inicie o cenário em modo E2 a partir da árvore `ns-3-dev`:

```bash
E2TERM_IP=$(kubectl get pods -n ricplt -o wide | grep e2term-alpha | awk '{print $6}')
cd ~/ns-3-dev
./ns3 run "nori-cmf --useE2=1 --ipE2TermRic=$E2TERM_IP"
```

Observe a mensagem `[INFO ] [SCTP] Sent E2-SETUP-REQUEST` para as 19 células na saída do próprio simulador, e confirme cruzando com o lado do RIC:

```bash
kubectl logs deployment-ricplt-e2mgr-* -n ricplt --tail=50 | grep CONNECTED
```

### 5. Reinscreva-se (resubscribe)

Como o `nori-cmf` registra suas 19 células *depois* que o pod do xApp já foi iniciado (os passos 2/3 aconteceram primeiro), a tentativa inicial de inscrição do xApp no passo 3 quase certamente não as alcançou. Dispare uma reinscrição:

```bash
bash resubscribe.sh
```

Essa chamada retorna imediatamente (`xApp ACKs resubscription request, resubscribing in the background`) — o ciclo real de cancelar-e-reinscrever roda em uma thread de segundo plano dentro do pod, não na própria requisição HTTP. Isso importa porque **um cluster que acumulou muitos registros de nós E2 obsoletos ao longo do tempo pode demorar um pouco para percorrer todos eles** (cada nó obsoleto/inacessível ainda custa até ~10 s até sua tentativa de inscrição desistir) — não assuma que "nenhuma linha de log imediata" significa falha; acompanhe os logs (tail) e dê um ou dois minutos em um cluster antigo/muito usado:

```bash
bash log_xapp.sh
```

Você quer ver `201, reason = Created` para os gNBs que sua execução atual do `nori-cmf` de fato registrou (os nomes têm o formato `gnb_<mcc>_<mnc>_<...>`; confirme cruzando com `kubectl get pods -n ricplt` → logs do e2mgr, ou simplesmente tente o próximo passo e veja se os dados aparecem).

### 6. Verifique se a malha de controle está de fato fechando

Uma vez inscrito em uma célula ativa, o xApp registra uma linha por célula por indicação *somente quando decide enviar um novo Hysteresis/TTT* (fica em silêncio quando sua decisão coincide com o que já está ativo — o que é a maioria das vezes depois que a malha se estabiliza):

```bash
bash log_xapp.sh | grep "Cell NRCellDU"
```

Uma malha saudável se parece com isto — note como o `(was hysteresis=... ttt=...)` de cada nova linha corresponde ao `-> hysteresis=... ttt=...` que a linha *anterior* daquela mesma célula enviou, confirmando que o simulador de fato aplicou o valor:

```text
Cell NRCellDU_12: ho=11 pp_ratio=27.3% rlf_ratio=9.1% -> hysteresis=1.0dB ttt=0.1s (was hysteresis=1.0 ttt=0.08)
Cell NRCellDU_12: ho=12 pp_ratio=25.0% rlf_ratio=8.3% -> hysteresis=1.0dB ttt=0.08s (was hysteresis=1.0 ttt=0.1)
```

Você pode conferir os mesmos valores diretamente na saída do próprio simulador, em `<outputDir>/bs-12.csv` (as colunas `hyst`/`ttt`) — eles devem coincidir exatamente.

### 7. Encerre

```bash
dms_cli uninstall xappmro ricxapp
```

---

### Solução de problemas da plataforma

Se o xApp reportar que nenhum gNB está registrado e o `kubectl get pods -n ricplt` mostrar algo não saudável, tente reimplantar o Near-RT RIC:

```bash
bash redeploy_ric.sh
watch kubectl get pods -n ricplt   # aguarde todo pod atingir 1/1 ou 2/2
```

**Problema conhecido — o `e2term` pode travar (crash) ao encerrar a simulação.** Quando o `nori-cmf` é finalizado, as 19 conexões SCTP com o `e2term` se fecham em rápida sucessão; isso pode disparar um crash pré-existente no próprio `e2term` do RIC (`free(): invalid pointer`), sem relação com este xApp ou com o `nori-cmf.cc`. Ocorre de forma intermitente (observado em aproximadamente 2 a cada 3 encerramentos durante o desenvolvimento), e o Kubernetes reinicia o pod automaticamente — leva cerca de 1 a 2 minutos (a readiness probe tem um atraso de inicialização) para voltar a `1/1`:

```bash
watch kubectl get pods -n ricplt deployment-ricplt-e2term-alpha-*
```

Não inicie uma nova execução do `nori-cmf` contra ele antes de voltar a `1/1`.

### Solução de problemas do xApp

- **`CrashLoopBackOff` logo após a instalação**: `kubectl logs -n ricxapp <pod> --previous` quase sempre mostra o traceback Python. Se for um `ImportError`, o build da imagem provavelmente falhou no meio do caminho (verifique `docker images | grep xappmro` e rode `update_xapp.sh` novamente).
- **Pod reinicia com código de saída 137 (`kubectl describe pod ... | grep -A3 "Last State"`)**: isso é um `SIGKILL` emitido pelo Kubernetes, quase sempre porque a liveness probe (`GET /ric/v1/health/alive`, timeout de 1 s) ficou sem resposta por tempo demais. Os caminhos de subscribe/unsubscribe/resubscribe usam um timeout por requisição e despacham o ciclo de reinscrição para uma thread de segundo plano justamente para evitar isso; se você ainda assim observar o problema, provavelmente o pod está sob pressão real de recursos (verifique `kubectl top pod -n ricxapp`, e a memória geral do nó com `free -h` — uma VM pequena construindo imagens Docker e rodando a plataforma RIC ao mesmo tempo pode ficar apertada).
- **Às vezes os gNBs simplesmente não se registram mesmo com a plataforma parecendo saudável**: isso pode acontecer enquanto alguns componentes do Near-RT RIC ainda estão terminando suas próprias rotinas de inicialização logo após o `redeploy_ric.sh`. Pare e reinicie o `nori-cmf`, ou chame `bash resubscribe.sh` novamente assim que os nós E2 aparecerem nos logs do `kubectl logs` no `e2mgr`.

## Observabilidade

Se `self.save_influx = True` (padrão, ver `src/custom_xapp.py`), cada indicação é gravada no InfluxDB em duas medições:

- `cell_metrics` — o dump bruto de KPM (mesmo formato do `xapp-nori`), um campo por `pmType` reportado pela célula;
- `mro_control` — um ponto por célula por indicação com `ho_total`, `pp_total`, `rlf_total`, `pp_ratio`, `rlf_ratio`, `current_hysteresis_db`, `current_ttt_s`, `new_hysteresis_db`, `new_ttt_s` e (quando reportado) `current_cio_db` — tudo o que é necessário para plotar a malha de controle do MRO ao longo do tempo no Grafana.
