#!/bin/bash
# GGUF -> llama.cpp server (OpenAI-совместимый API на порту __PORT__)
# Нужен devel-образ CUDA: сервер собирается из исходников с поддержкой GPU.
# Параметры из env заказа Clore: MODEL_REPO, MODEL_NAME, API_KEY, HF_TOKEN, CTX, GGUF_FILE
exec > >(tee -a /root/deploy.log >/dev/null) 2>&1
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

# Трассировку включаем только здесь: выше присваиваются ключи, и с set -x они
# попадали бы в /root/deploy.log, а оттуда — в лог деплоя на вашем ПК
set -x

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
  ( set +x; export HF_TOKEN="${HF_TOKEN}"; export HF_HUB_ENABLE_HF_TRANSFER=1; export HF_XET_HIGH_PERFORMANCE=1
    if [ -n "${GGUF_FILE}" ]; then
      # у шардированных GGUF качаем все части: file-00001-of-0000N -> маска по префиксу
      PATTERN=$(echo "${GGUF_FILE}" | sed -E 's/-0*[0-9]+-of-0*[0-9]+\.gguf$/*.gguf/')
      hf_download "${MODEL_REPO}" --include "${PATTERN}" --local-dir "${MODEL_DIR}" >> /root/download.log 2>&1
    else
      hf_download "${MODEL_REPO}" --local-dir "${MODEL_DIR}" >> /root/download.log 2>&1
    fi
    touch /root/.download_done
    # веса на месте, а движок ещё собирается — пусть это будет видно, а не «100%»
    [ -f /root/.build_done ] || [ "$(cat /root/.stage 2>/dev/null)" != download ] || stage build ) &
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

  git_clone_retry https://github.com/ggml-org/llama.cpp /root/llama.cpp \
    || { echo "[setup] ОШИБКА: исходники llama.cpp недоступны"; exit 1; }
  cmake -S /root/llama.cpp -B /root/llama.cpp/build \
    -DGGML_CUDA=ON -DLLAMA_CURL=ON -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_CUDA_COMPILER="${CUDACXX}" \
    -DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCH}" \
    || { echo "[setup] ОШИБКА: cmake не сконфигурировал сборку"; exit 1; }
  cmake --build /root/llama.cpp/build --config Release -j"$(nproc)" --target llama-server \
    || { echo "[setup] ОШИБКА: llama-server не собрался"; exit 1; }
  touch /root/.build_done
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

# Сколько слоёв держать на картах. Раньше всегда было -ngl 999 — и оно отключало
# встроенный подбор llama.cpp (--fit): на 8× RTX PRO 5000 веса GLM-5.3 Q8 влезли, а
# буферам вычислений (4 ГиБ на карту) места не хватило, и движок упал. Без -ngl он
# сам раскладывает слои с учётом буферов и кэша, а при нехватке переносит часть
# экспертов в RAM: медленнее, но модель поднимается. С явной выгрузкой экспертов
# подбор всё равно не работает, там остаётся -ngl 999.
NGL_ARG=""
if [ -n "${MOE_ARG}" ] || ! /root/llama.cpp/build/bin/llama-server --help 2>&1 | grep -q -- "--fit"; then
  NGL_ARG="-ngl 999"
fi

stage engine
echo "[setup] запускаю модель из ${GGUF}"
{ set +x; } 2>/dev/null   # дальше без трассировки: в команде ключ API
setsid nohup /root/llama.cpp/build/bin/llama-server \
  -m "${GGUF}" \
  --alias "${MODEL_NAME}" \
  --host 0.0.0.0 --port "${PORT}" \
  --api-key "${API_KEY}" \
  -c "${CTX}" ${NGL_ARG} --split-mode layer \
  ${MOE_ARG} \
  --parallel 1 --metrics \
  --flash-attn on --jinja \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --temp 0.7 --top-p 0.95 --min-p 0.02 > /root/engine.log 2>&1 < /dev/null &
set -x
# Движок, упавший при загрузке модели (неизвестная архитектура, нехватка памяти), раньше
# выглядел как «загружает веса» ещё 5 минут, пока деплой не замечал застой. Следим, пока
# он не ответит: умер раньше — сразу провал с причиной из его лога
( for i in $(seq 1 1440); do
    curl -fs -o /dev/null "http://127.0.0.1:${PORT}/health" && exit 0
    if ! pgrep -f /root/llama.cpp/build/bin/llama-server >/dev/null; then
      sleep 2
      if grep -a -q -E 'out of memory|failed to allocate' /root/engine.log; then
        # последние строки тут — общее «failed to create context», суть выше
        why="не хватило видеопамяти: $(grep -a -E 'out of memory|failed to allocate' /root/engine.log | head -n 1 | cut -c1-200)"
      else
        why=$(grep -a -E ' E |error' /root/engine.log | tail -n 3 | tr '\n' ' ' | cut -c1-260)
      fi
      echo "ОШИБКА: движок не запустился: ${why}" | tee -a /root/deploy.log > /root/.fail
      stage failed
      exit 0
    fi
    sleep 5
  done ) &
# Эксперты читаются из оперативки только после того, как файл окажется в страничном
# кэше. Иначе первые минуты они подтягиваются с диска, и скорость вдвое ниже рабочей.
if [ -n "${N_CPU_MOE}" ] && [ "${N_CPU_MOE}" -gt 0 ] 2>/dev/null; then
  echo "[setup] прогреваю страничный кэш весами (${GGUF})"
  ( cat "${GGUF}" > /dev/null 2>&1 & )
fi
echo "ONSTART DONE $(date)"
