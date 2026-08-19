# xapp-CMF

O xApp-CMF é um xApp OSC que implementa o **Conflict Mitigation Framework (CMF)** proposto por Adamczyk & Kliks em *"Conflict Mitigation Framework and Conflict Detection in O-RAN Near-RT RIC"* (IEEE ComMag 2023, [arXiv:2305.07117](https://arxiv.org/abs/2305.07117)), como um xApp autônomo no Near-RT RIC — no mesmo lugar em que o próprio artigo o posiciona, em vez de embuti-lo no cenário ns-3 [`nori-cmf.cc`](../ns-3-dev/contrib/nori/examples/nori-cmf.cc).

Ele é o terceiro xApp deste projeto, ao lado do [`xapp-MRO`](../xapp-MRO) e do [`xapp-MLB`](../xapp-MLB): esses dois propõem alterações nos limites de handover, e este é quem decide se uma proposta que conflita com a decisão de outro xApp pode de fato chegar à RAN.

## Por que um xApp separado

O `nori-cmf.cc` costumava incluir sua própria lógica de detecção/mitigação de conflitos, diretamente dentro do processo ns-3. Não é assim que o artigo de referência define o CMF: ele é explicitamente um componente da entidade de Conflict Mitigation (CM) do Near-RT RIC (Fig. 2 do artigo), independente de qualquer implementação particular de RAN ou de nó E2, e destinado a mitigar conflitos entre **quaisquer** xApps implantados naquele RIC — não apenas os que um determinado simulador conhece. O `nori-cmf.cc` agora é *apenas* o cenário de RAN + nó E2: ele aplica qualquer RIC Control Request que receba, de qualquer xApp que a tenha enviado, sem nenhuma arbitragem própria. Toda a lógica de detecção e resolução de conflitos agora vive aqui.

## Os três módulos

Seguindo exatamente a Seção III do artigo:

| Módulo | Arquivo | Função |
|---|---|---|
| **CD Agent** (Conflict Detection Agent) | [`src/cd_agent.py`](src/cd_agent.py) | Implementa o **DCD** (Direct Conflict Detection) e o **ICD** (Indirect Conflict Detection). Ambos são *pré-ação*: toda decisão proposta é verificada contra uma base de decisões atualmente em vigor antes de ser autorizada a entrar em efeito. |
| **CR Agent** (Conflict Resolution Agent) | [`src/cr_agent.py`](src/cr_agent.py) | Decide, para uma proposta sinalizada como conflitante pelo CD Agent, se ela é permitida. Implementa o mesmo esquema simples de priorização que o artigo avalia: `none` (apenas detecta), `prioMRO`, `prioMLB`. |
| **PMon** (Performance Monitoring) | [`src/pmon.py`](src/pmon.py) | Alimenta o **ImCD** (Implicit Conflict Detection), que é inerentemente *pós-ação*: observa um KPI da RAN (satisfação média das UEs por célula) e, quando ele cai de forma significativa, correlaciona a queda com o próprio registro do CD Agent sobre quais xApps atuaram recentemente naquela célula. |

### DCD — Detecção Direta de Conflitos

Dois xApps escrevendo o **mesmo parâmetro** da mesma célula com valores diferentes. Na implantação atual (apenas MRO e MLB, escrevendo parâmetros disjuntos) isso não deveria ocorrer — está implementado por completude e como a salvaguarda (*fail-safe*) que o artigo descreve ("em caso de erro humano que leve à implantação de xApps diretamente conflitantes"), e disparará corretamente se um segundo xApp algum dia direcionar `HOHysteresis`, `HOTimeToTrigger` ou `HOMeasurementOffset`.

### ICD — Detecção Indireta de Conflitos

Dois xApps escrevendo **parâmetros diferentes do mesmo grupo funcional**. `HOMeasurementOffset` (CIO), `HOHysteresis` e `HOTimeToTrigger` estão todos registrados sob o grupo `CellAffectHandoverBoundary` (ver `PARAMETER_GROUPS` em `cd_agent.py`), pois juntos decidem quando um handover é disparado. Este é o tipo de conflito que ocorre estruturalmente entre MRO e MLB nesta implantação, e o que é exercitado em praticamente toda rodada de controle em que ambos estão ativos.

### ImCD — Detecção Implícita de Conflitos

Não é capaz de impedir que uma decisão conflitante entre em efeito — quando é detectada, ela já ocorreu. O PMon acompanha `QoS.MeanUeSatisfactionPermille` por célula (reportado pelo `nori-cmf.cc` em cada indicação KPM); uma queda relativa acima de um limiar é verificada contra a base de dados do CD Agent para aquela célula, e se mais de um xApp tinha uma decisão em vigor ali, um contador de ocorrências correlacionadas é incrementado. Somente após algumas ocorrências correlacionadas (não uma única amostra ruidosa) o conflito implícito é de fato reportado.

## Como o MRO e o MLB se comunicam com este xApp

Não há uma forma prática de este xApp interceptar de modo transparente uma mensagem RMR `RIC_CONTROL_REQ` endereçada ao `e2term`: o MRO e o MLB direcionam um nó/célula E2 *específico* respondendo (`rmr_rts`) pela mesma conexão em que sua indicação KPM chegou, e essa rota de resposta só pode ser usada pelo processo que recebeu a mensagem original — um terceiro processo não pode retransmiti-la em nome deles sem perder o contexto de roteamento.

Em vez disso, este xApp expõe um endpoint HTTP síncrono, e o MRO/MLB o chamam **antes** de sequer montar ou enviar uma RIC Control Request:

```
POST /ric/v1/cmf/evaluate
{"source": "MRO" | "MLB", "cellId": <int>, "parameterId": 1 | 2 | 3, "value": <float>}

-> 200 {"allowed": true}
-> 200 {"allowed": false, "reason": "ICD indirect conflict with MLB's 'HOMeasurementOffset'=3.0 on cell 12 (prioritized: MLB)"}
```

`parameterId` segue a mesma convenção já decodificada por `RicControlCallback()` no `nori-cmf.cc`: `1` = `HOMeasurementOffset` (CIO), `2` = `HOHysteresis`, `3` = `HOTimeToTrigger`. Se uma proposta for rejeitada, o MRO/MLB simplesmente não envia a RIC Control Request daquele parâmetro nesta rodada — exatamente como se o CMF a tivesse descartado silenciosamente, reproduzindo o comportamento antigo (embutido), exceto que agora a decisão é bloqueada **antes** de chegar à RAN, não depois.

Essa chamada tem um timeout curto e **falha aberta** (*fails open*): se o xApp CMF estiver inacessível (não implantado, ainda inicializando, ou em crash), o MRO/MLB prosseguem como se a mitigação de conflitos estivesse desabilitada, em vez de travar todo o controle da RAN por causa de uma dependência ausente. Um aviso é registrado (log) toda vez que isso acontece.

Independentemente do endpoint de avaliação, este xApp também se inscreve em KPM (RAN Function 200) em todo nó E2 registrado, unicamente para alimentar o PMon — ele nunca escreve nenhum parâmetro de RAN por conta própria.

## Configurando o modo de resolução

Definido em `src/custom_xapp.py`:

```python
CM_MODE = CrMode.PRIO_MRO  # ou CrMode.PRIO_MLB, ou CrMode.NONE
```

correspondendo aos três modos que a própria avaliação do artigo compara (`CMF desabilitado`, `priorizar MRO`, `priorizar MLB`).

## Requisitos

Os mesmos do [`xapp-MRO`](../xapp-MRO/README.md#requisitos) e do [`xapp-MLB`](../xapp-MLB/README.md#requisitos) — uma VM do OpenRAN@Brasil Blueprint com um Near-RT RIC saudável e um registro local de imagens em `127.0.0.1:5001`.

## Embarcando no Near-RT RIC

Siga o mesmo passo a passo do [README do `xapp-MRO`](../xapp-MRO/README.md#embarcando-no-near-rt-ric-passo-a-passo) (build/instalação via `bash update_xapp.sh`, confirmar o pod, reinscrever assim que o `nori-cmf` estiver conectado), substituindo todo `xappmro` por `xappcmf`. **Implante este xApp antes, ou ao mesmo tempo, que o MRO/MLB** — enquanto ele estiver ausente, o MRO/MLB ainda funcionam (falha aberta), mas nenhum conflito é de fato mitigado, apenas o que cada xApp decidiu por conta própria.

## Observabilidade

- O `kubectl logs` mostra uma linha por proposta avaliada que resultou em conflito, por exemplo: `Cell 12: MLB proposes HOMeasurementOffset=3.0 -> REJECTED (ICD indirect conflict with MRO's 'HOHysteresis'=1.5 on cell 12 (prioritized: MRO))`.
- Todo conflito detectado (DCD, ICD ou ImCD) é anexado como uma linha JSON em `/tmp/conflicts.json` dentro do pod, no mesmo formato de mensagem de `json_messages/{DCD,ICD,ImCD}/*signal conflict.json` no repositório de referência ([`czezy/O-RAN_CMF_CM2023`](https://github.com/czezy/O-RAN_CMF_CM2023)).
- Se `self.save_influx = True` (padrão), toda avaliação é gravada no InfluxDB na medição `cmf_evaluations` (`cellId`, `source`, `parameter`, `value`, `conflicts`, `allowed`), e todo conflito implícito em `cmf_conflicts`.
