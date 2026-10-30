#!/bin/bash
# safetensors (fp16 / AWQ / GPTQ / FP8) -> vLLM (OpenAI-совместимый API на порту __PORT__)
# Параметры из env заказа Clore: MODEL_REPO, MODEL_NAME, API_KEY, HF_TOKEN, CTX, QUANT
exec > >(tee -a /root/deploy.log >/dev/null) 2>&1
set -x
export DEBIAN_FRONTEND=noninteractive
cd /root

MODEL_REPO="${MODEL_REPO:-__MODEL_REPO__}"
MODEL_NAME="${MODEL_NAME:-__MODEL_NAME__}"
API_KEY="${API_KEY:-__API_KEY__}"
CTX="${CTX:-__CTX__}"
QUANT="${QUANT:-__QUANT__}"
TOOL_PARSER="${TOOL_PARSER:-__TOOL_PARSER__}"
PORT="__PORT__"
MODEL_DIR="/root/models/${MODEL_NAME}"

__COMMON__

start_status
stage packages

if [ ! -f /root/.setup_done ]; then
  install_base git curl python3 python3-venv python3-pip pciutils
  install_hf
  need python3 pip3
  start_status
  stage download

  mkdir -p "${MODEL_DIR}"
  ( export HF_TOKEN="${HF_TOKEN}"; export HF_HUB_ENABLE_HF_TRANSFER=1; export HF_XET_HIGH_PERFORMANCE=1
    hf_download "${MODEL_REPO}" --local-dir "${MODEL_DIR}" >> /root/download.log 2>&1
    touch /root/.download_done ) &
  DLPID=$!

  python3 -m venv /root/venv && . /root/venv/bin/activate && pip install -q -U pip
  # Без прогресса этот шаг выглядит как зависание: vLLM с зависимостями тянет около
  # 3 ГБ, на медленном зеркале это больше часа, а с -q в логе не видно ровно ничего.
  echo "[setup] ставлю vLLM (несколько ГБ, может занять 20-60 минут)"
  pip install --progress-bar off vllm \
    || { echo "[setup] ОШИБКА: не удалось поставить vLLM"; exit 1; }
  echo "[setup] vLLM установлен: $(pip show vllm 2>/dev/null | sed -n 's/^Version: //p')"
  deactivate
  wait $DLPID
  [ -f /root/.download_done ] || { echo "[setup] ОШИБКА: веса не скачались, движок не запускаю"; exit 1; }
  touch /root/.setup_done
fi

fix_cuda_compat

GPUS=$(nvidia-smi -L | wc -l)
# tensor-parallel должен быть степенью двойки и делить число GPU (иначе vLLM не стартует),
# остальные карты добираем pipeline-параллелизмом — так задействуются все GPU при 3, 5, 6, 12…
TP=1
for N in 8 4 2 1; do
  if [ "$N" -le "$GPUS" ] && [ $((GPUS % N)) -eq 0 ]; then TP=$N; break; fi
done
PP=$((GPUS / TP))
QUANT_ARG=""
[ -n "${QUANT}" ] && QUANT_ARG="--quantization ${QUANT}"

# Своя реализация all-reduce у vLLM требует работающего P2P между картами. На
# потребительских GeForce и RTX PRO его нет (nvidia-smi topo даёт PHB), и движок падает
# с «Cuda error custom_all_reduce.cuh: invalid argument» — причём уже ПОСЛЕ скачивания
# весов, то есть через час ожидания. На NVLink-железе отключение почти ничего не стоит.
ALLREDUCE_ARG=""
if [ "${TP}" -gt 1 ]; then
  ALLREDUCE_ARG="--disable-custom-all-reduce"
  export VLLM_DISABLE_CUSTOM_ALL_REDUCE=1
  echo "[setup] tensor-parallel ${TP}: отключаю custom all-reduce (нужен P2P между картами)"
fi

. /root/venv/bin/activate

# vLLM с версии 0.32 на старте JIT-компилирует сэмплер FlashInfer, а для этого нужен nvcc.
# В runtime-образе CUDA тулкита нет, и движок падает с «nvcc: not found» уже после загрузки
# весов — то есть после получаса ожидания. Штатный сэмплер работает без компиляции.
export VLLM_USE_FLASHINFER_SAMPLER="${VLLM_USE_FLASHINFER_SAMPLER:-0}"
# без этого фрагментация аллокатора отъедает сотни мегабайт и роняет загрузку по OOM
export PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"

# Парсер tool-calls зависит от семейства модели; если версия vLLM его не знает, откатываемся
# на hermes. ВАЖНО: список флагов есть только в `--help=all` — у обычного `--help` в свежих
# версиях это короткая страница с группами конфигов, и проверка всегда давала ложный
# отрицательный ответ, молча ломая вызовы инструментов для всех моделей.
if [ -z "${TOOL_PARSER}" ] || ! vllm serve --help=all 2>/dev/null | grep -q -- "${TOOL_PARSER}"; then
  echo "tool-call-parser ${TOOL_PARSER} недоступен, беру hermes"
  TOOL_PARSER="hermes"
fi
stage engine
setsid nohup vllm serve "${MODEL_DIR}" \
  --served-model-name "${MODEL_NAME}" \
  --host 0.0.0.0 --port "${PORT}" \
  --api-key "${API_KEY}" \
  --max-model-len "${CTX}" \
  --tensor-parallel-size "${TP}" \
  --pipeline-parallel-size "${PP}" \
  ${ALLREDUCE_ARG} \
  --gpu-memory-utilization 0.92 \
  --enable-auto-tool-choice --tool-call-parser "${TOOL_PARSER}" \
  ${QUANT_ARG} > /root/engine.log 2>&1 < /dev/null &
echo "ONSTART DONE $(date)"
