#!/bin/bash

# Executa o exemplo nori-cmf (cenário de rede: 19 células + 380 UEs + E2).
#
# Este exemplo NÃO implementa mais o Conflict Mitigation Framework — ele só
# simula a RAN e aplica, sem arbitrar, qualquer decisão de controle recebida
# (do xApp-MRO/xApp-MLB reais via E2, ou da emulação interna quando --useE2=0).
# O CMF (CD Agent, CR Agent, PMon) roda como um xApp separado: veja xapp-CMF.
#
# Uso:
#   ./run_nori_cmf.sh                              # offline, 1000 s, emulação interna de MRO/MLB
#   ./run_nori_cmf.sh --use-e2                      # conecta ao Near-RT RIC via E2 (kubectl)
#   ./run_nori_cmf.sh --sim-time 200 --warmup-time 50 --output-dir /tmp/cmf-teste
#
# Qualquer opção não reconhecida é repassada como está para o ns3 (ex.: --nUe=100).

set -e

DEPLOYMENT_NAME="deployment-ricplt-e2term-alpha"
NAMESPACE="ricplt"

SIM_TIME="1000"
WARMUP_TIME="150"
OUTPUT_DIR="nori-cmf-output"
USE_E2=0
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --sim-time)
            SIM_TIME="$2"
            shift 2
            ;;
        --warmup-time)
            WARMUP_TIME="$2"
            shift 2
            ;;
        --output-dir)
            OUTPUT_DIR="$2"
            shift 2
            ;;
        --use-e2)
            USE_E2=1
            shift
            ;;
        *)
            EXTRA_ARGS+=("$1")
            shift
            ;;
    esac
done

IP_ARG=()
if [[ "$USE_E2" -eq 1 ]]; then
    echo "Procurando o pod associado ao deployment '${DEPLOYMENT_NAME}' no namespace '${NAMESPACE}'..."

    # Recupera o IP do pod correspondente ao deployment
    POD_IP=$(kubectl get pods -n "$NAMESPACE" -o wide | grep "$DEPLOYMENT_NAME" | awk '{print $6}')

    if [[ -n "$POD_IP" ]]; then
        echo "O IP do pod é: $POD_IP"
    else
        echo "Não foi possível encontrar o IP do pod. Verifique se o deployment está correto."
        exit 1
    fi

    IP_ARG=(--useE2=1 --ipE2TermRic="$POD_IP")
else
    IP_ARG=(--useE2=0)
fi

echo "==> nori-cmf: simTime=${SIM_TIME}s warmupTime=${WARMUP_TIME}s outputDir=${OUTPUT_DIR}"
./ns3 run "nori-cmf ${IP_ARG[*]} --simTime=${SIM_TIME} --warmupTime=${WARMUP_TIME} --outputDir=${OUTPUT_DIR} ${EXTRA_ARGS[*]}"
