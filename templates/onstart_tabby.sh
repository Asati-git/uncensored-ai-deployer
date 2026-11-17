#!/bin/bash
# EXL3/EXL2 -> TabbyAPI (OpenAI-совместимый API на порту __PORT__)
# Параметры приходят из env заказа Clore: MODEL_REPO, MODEL_NAME, API_KEY, HF_TOKEN, CTX, WEIGHT_FORMAT
exec > >(tee -a /root/deploy.log >/dev/null) 2>&1
export DEBIAN_FRONTEND=noninteractive
cd /root

MODEL_REPO="${MODEL_REPO:-__MODEL_REPO__}"
MODEL_NAME="${MODEL_NAME:-__MODEL_NAME__}"
API_KEY="${API_KEY:-__API_KEY__}"
CTX="${CTX:-__CTX__}"
WEIGHT_FORMAT="${WEIGHT_FORMAT:-__FORMAT__}"
PORT="__PORT__"
# по этому каталогу статус-порт считает скачанное; без него прогресс был всегда 0
MODEL_DIR="/root/tabbyAPI/models/${MODEL_NAME}"

# Трассировку включаем только здесь: выше присваиваются ключи, и с set -x они
# попадали бы в /root/deploy.log, а оттуда — в лог деплоя на вашем ПК
set -x

__COMMON__

start_status
stage packages

if [ ! -f /root/.setup_done ]; then
  install_base git curl python3 python3-venv python3-pip pciutils
  install_hf
  need git python3 pip3
  start_status
  stage download
  git_clone_retry https://github.com/theroyallab/tabbyAPI /root/tabbyAPI \
    || { echo "[setup] ОШИБКА: исходники TabbyAPI недоступны"; exit 1; }
  cd /root/tabbyAPI
  echo '{"gpu_lib": "cu12", "first_run_done": true}' > start_options.json

  cat > config.yml <<CFG
network:
  host: 0.0.0.0
  port: ${PORT}
  disable_auth: false
  allowed_origins: ["*"]
  api_servers: ["OAI"]
logging:
  log_prompt: false
  log_generation_params: false
model:
  model_dir: models
  inline_model_loading: false
  model_name: ${MODEL_NAME}
  max_seq_len: ${CTX}
  cache_mode: Q8
  gpu_split_auto: true
  autosplit_reserve: [1024]
  chunk_size: 4096
  max_batch_size: 4
sampling:
  override_preset: uncensored
CFG

  mkdir -p sampler_overrides
  cat > sampler_overrides/uncensored.yml <<'SMP'
temperature:
  override: 0.7
  force: true
top_p:
  override: 0.95
  force: true
min_p:
  override: 0.02
  force: true
top_k:
  override: 0
  force: true
SMP

  cat > api_tokens.yml <<TOK
api_key: ${API_KEY}
admin_key: ${API_KEY}
TOK

  # веса качаются параллельно с установкой зависимостей
  mkdir -p models
  ( set +x; export HF_TOKEN="${HF_TOKEN}"; export HF_HUB_ENABLE_HF_TRANSFER=1; export HF_XET_HIGH_PERFORMANCE=1
    hf_download "${MODEL_REPO}" --local-dir "/root/tabbyAPI/models/${MODEL_NAME}" >> /root/download.log 2>&1
    touch /root/.download_done ) &
  DLPID=$!

  python3 -m venv venv && . venv/bin/activate && pip install -q -U pip
  pip install -q -e ".[cu12]"
  # exllamav3 ставится вместе с TabbyAPI (экстра cu12) — той версии, которую он требует.
  # Раньше поверх ставилось закреплённое колесо 1.5.4, и свежий TabbyAPI отказывался
  # стартовать: «requires exllamav3 1.6.6 or greater». Доустанавливаем, только если нет.
  if [ "${WEIGHT_FORMAT}" = "exl3" ]; then
    python -c "import exllamav3" 2>/dev/null || pip install -q exllamav3       || { echo "[setup] ОШИБКА: не удалось поставить exllamav3"; exit 1; }
  fi
  deactivate
  wait $DLPID
  [ -f /root/.download_done ] || { echo "[setup] ОШИБКА: веса не скачались, движок не запускаю"; exit 1; }
  touch /root/.setup_done
fi

fix_cuda_compat

cd /root/tabbyAPI
stage engine
setsid nohup ./start.sh > /root/engine.log 2>&1 < /dev/null &
echo "ONSTART DONE $(date)"
