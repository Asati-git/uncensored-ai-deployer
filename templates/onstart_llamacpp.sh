#!/bin/bash
# GGUF -> llama.cpp server (OpenAI-совместимый API на порту __PORT__)
# Нужен devel-образ CUDA: сервер собирается из исходников с поддержкой GPU.
# Параметры из env заказа Clore: MODEL_REPO, MODEL_NAME, API_KEY, HF_TOKEN, CTX, GGUF_FILE
exec > >(tee -a /root/deploy.log >/dev/null) 2>&1
set -x
export DEBIAN_FRONTEND=noninteractive
cd /root

MODEL_REPO="${MODEL_REPO:-__MODEL_REPO__}"
MODEL_NAME="${MODEL_NAME:-__MODEL_NAME__}"
API_KEY="${API_KEY:-__API_KEY__}"
CTX="${CTX:-__CTX__}"
GGUF_FILE="${GGUF_FILE:-__GGUF_FILE__}"
# Сколько слоёв MoE-экспертов оставить в оперативке. Считается в deploy.py под VRAM
# конкретного сервера и приезжает в env заказа; 0 — вся модель на карте.
N_CPU_MOE="${N_CPU_MOE:-__N_CPU_MOE__}"
PORT="__PORT__"
MODEL_DIR="/root/models/${MODEL_NAME}"

__COMMON__

# если python3 уже есть в образе — отдаём прогресс с первой секунды
start_status
stage packages

if [ ! -f /root/.setup_done ]; then
  install_base git curl cmake build-essential python3 python3-pip libcurl4-openssl-dev pciutils
  install_hf
  need git cmake pip3
  start_status
  stage download

  mkdir -p "${MODEL_DIR}"
  ( export HF_TOKEN="${HF_TOKEN}"; export HF_HUB_ENABLE_HF_TRANSFER=1; export HF_XET_HIGH_PERFORMANCE=1
    if [ -n "${GGUF_FILE}" ]; then
      # у шардированных GGUF качаем все части: file-00001-of-0000N -> маска по префиксу
      PATTERN=$(echo "${GGUF_FILE}" | sed -E 's/-0*[0-9]+-of-0*[0-9]+\.gguf$/*.gguf/')
      hf_download "${MODEL_REPO}" --include "${PATTERN}" --local-dir "${MODEL_DIR}" >> /root/download.log 2>&1
    else
      hf_download "${MODEL_REPO}" --local-dir "${MODEL_DIR}" >> /root/download.log 2>&1
    fi
    touch /root/.download_done ) &
  DLPID=$!

  # onstart выполняется не из login-шелла, поэтому /usr/local/cuda/bin в PATH не попадает
  # и cmake не находит nvcc: «No CMAKE_CUDA_COMPILER could be found»
  fix_cuda_compat
  export PATH="/usr/local/cuda/bin:${PATH}"
  CUDACXX="$(command -v nvcc || true)"
  [ -n "${CUDACXX}" ] || { echo "[setup] ОШИБКА: в образе нет nvcc — нужен devel-образ CUDA"; exit 1; }
  export CUDACXX

  # llama.cpp по умолчанию собирается под -arch=native, но внутри контейнера nvcc
  # не всегда видит карту и молча берёт запасную архитектуру — бинарь получается
  # либо нерабочим, либо медленным из-за JIT. Спрашиваем compute capability у драйвера.
  CUDA_ARCH="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null \
    | tr -d ' .' | sort -u | paste -sd';')"
  [ -n "${CUDA_ARCH}" ] || CUDA_ARCH="80;86;89;90"
  echo "[setup] собираю llama.cpp под архитектуры: ${CUDA_ARCH}"

  git_clone_retry https://github.com/ggml-org/llama.cpp /root/llama.cpp \n    || { echo "[setup] ОШИБКА: исходники llama.cpp недоступны"; exit 1; }
  cmake -S /root/llama.cpp -B /root/llama.cpp/build \
    -DGGML_CUDA=ON -DLLAMA_CURL=ON -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_CUDA_COMPILER="${CUDACXX}" \
    -DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCH}" \
    || { echo "[setup] ОШИБКА: cmake не сконфигурировал сборку"; exit 1; }
  cmake --build /root/llama.cpp/build --config Release -j"$(nproc)" --target llama-server \
    || { echo "[setup] ОШИБКА: llama-server не собрался"; exit 1; }
  wait $DLPID
  [ -f /root/.download_done ] || { echo "[setup] ОШИБКА: веса не скачались, движок не запускаю"; exit 1; }
  touch /root/.setup_done
fi

fix_cuda_compat

# Грузим именно выбранный квант. Раньше тут был «первый по алфавиту .gguf в каталоге»,
# и если маска --include затягивала соседний файл или в каталоге оставалось что-то
# от прошлого прогона, движок молча поднимал не ту модель.
GGUF="${MODEL_DIR}/${GGUF_FILE}"
if [ -z "${GGUF_FILE}" ] || [ ! -f "${GGUF}" ]; then
  GGUF=$(find "${MODEL_DIR}" -name '*.gguf' ! -name 'mmproj*' ! -name '*imatrix*' | sort | head -n1)
  echo "[setup] выбранный файл не найден, беру ${GGUF}"
fi
[ -n "${GGUF}" ] && [ -f "${GGUF}" ] || { echo "[setup] ОШИБКА: не нашёл ни одного .gguf в ${MODEL_DIR}"; exit 1; }
# У MoE-модели на каждый токен работает лишь малая часть экспертов, поэтому их можно
# держать в обычной RAM и считать на CPU. Замерено на RTX 3090: 155 ток/с против 24 при
# полной выгрузке — это способ поднять модель, которая иначе не влезает, а не ускорение.
# Флаг --n-cpu-moe есть не во всех сборках, поэтому для старых остаётся запасной путь
# через --override-tensor с явным перечислением слоёв.
MOE_ARG=""
if [ -n "${N_CPU_MOE}" ] && [ "${N_CPU_MOE}" -gt 0 ] 2>/dev/null; then
  if /root/llama.cpp/build/bin/llama-server --help 2>&1 | grep -q -- "--n-cpu-moe"; then
    MOE_ARG="--n-cpu-moe ${N_CPU_MOE}"
  else
    LAYERS=$(seq 0 $((N_CPU_MOE - 1)) | paste -sd'|')
    MOE_ARG="--override-tensor blk\.(${LAYERS})\.ffn_(up|down|gate)_exps\.weight=CPU"
    echo "[setup] эта сборка не знает --n-cpu-moe, беру --override-tensor"
  fi
  echo "[setup] эксперты первых ${N_CPU_MOE} слоёв остаются в оперативке"
fi

stage engine
echo "[setup] запускаю модель из ${GGUF}"
setsid nohup /root/llama.cpp/build/bin/llama-server \
  -m "${GGUF}" \
  --alias "${MODEL_NAME}" \
  --host 0.0.0.0 --port "${PORT}" \
  --api-key "${API_KEY}" \
  -c "${CTX}" -ngl 999 --split-mode layer \
  ${MOE_ARG} \
  --parallel 1 --metrics \
  --flash-attn on --jinja \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --temp 0.7 --top-p 0.95 --min-p 0.02 > /root/engine.log 2>&1 < /dev/null &
# Эксперты читаются из оперативки только после того, как файл окажется в страничном
# кэше. Иначе первые минуты они подтягиваются с диска, и скорость вдвое ниже рабочей.
if [ -n "${N_CPU_MOE}" ] && [ "${N_CPU_MOE}" -gt 0 ] 2>/dev/null; then
  echo "[setup] прогреваю страничный кэш весами (${GGUF})"
  ( cat "${GGUF}" > /dev/null 2>&1 & )
fi
echo "ONSTART DONE $(date)"
