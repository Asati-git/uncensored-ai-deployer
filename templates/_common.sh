# Общая преамбула startup-скриптов: render_onstart подставляет её в каждый шаблон.
#
# Зачем: Clore в момент запуска onstart сам доустанавливает пакеты и держит apt-блокировку.
# Без ожидания `apt-get update` падает с «Could not get lock», следом из-за && не отрабатывает
# `apt-get install`, и дальше по цепочке нет ни pip3, ни git, ни cmake — а скрипт этого
# не замечает и уходит в бесконечный цикл повторов скачивания.

# Есть ли в системе живой процесс с таким именем. Через /proc, потому что procps
# (pgrep/ps) в образе может быть не установлен — а проверка нужна до первого apt.
_proc_running() {
  local p name
  for p in /proc/[0-9]*; do
    [ -r "$p/comm" ] || continue
    [ "$p" = "/proc/$$" ] && continue
    name=$(cat "$p/comm" 2>/dev/null)
    for want in "$@"; do
      [ "$name" = "$want" ] && return 0
    done
  done
  return 1
}

# Clore при старте контейнера сам ставит пакеты, в том числе openssh-server.
# Если влезть в apt параллельно, его установка падает молча — и sshd не поднимается
# НИКОГДА: сервер остаётся без доступа, диагностировать деплой нечем.
# Поэтому сначала ждём, пока чужой apt закончит, и только потом работаем сами.
wait_for_entrypoint() {
  local i
  for i in $(seq 1 60); do
    _proc_running apt apt-get dpkg unattended-upgr || {
      echo "[setup] чужой apt завершился, продолжаю"
      break
    }
    echo "[setup] жду, пока Clore доустановит свои пакеты ($i/60)"
    sleep 10
  done
  # отдельно убеждаемся, что sshd успел подняться — иначе сервер будет недоступен
  for i in $(seq 1 30); do
    _proc_running sshd && { echo "[setup] sshd работает"; return 0; }
    sleep 5
  done
  ensure_sshd
}

# Если entrypoint Clore не поднял sshd (так бывает на хостах с низким reliability),
# сервер превращается в чёрный ящик: логи деплоя прочитать нечем. Поднимаем сами —
# ключ и пароль приезжают в env заказа из deploy.py.
ensure_sshd() {
  echo "[setup] sshd не работает — поднимаю сам"
  command -v sshd >/dev/null 2>&1 || apt_retry install -y -qq openssh-server || {
    echo "[setup] не удалось поставить openssh-server, SSH не будет"
    return 1
  }
  mkdir -p /var/run/sshd /root/.ssh
  chmod 700 /root/.ssh
  if [ -n "${SSH_KEY}" ]; then
    grep -qxF "${SSH_KEY}" /root/.ssh/authorized_keys 2>/dev/null \
      || echo "${SSH_KEY}" >> /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
  fi
  [ -n "${SSH_PASSWORD}" ] && echo "root:${SSH_PASSWORD}" | chpasswd 2>/dev/null
  sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config 2>/dev/null
  sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config 2>/dev/null
  ssh-keygen -A >/dev/null 2>&1
  /usr/sbin/sshd 2>&1 | tail -2
  sleep 2
  _proc_running sshd && echo "[setup] sshd поднят вручную" \
    || echo "[setup] ВНИМАНИЕ: sshd поднять не удалось, сервер будет недоступен по SSH"
}

# apt с ожиданием чужой блокировки и повторами
apt_retry() {
  for i in $(seq 1 30); do
    if apt-get -o DPkg::Lock::Timeout=120 "$@"; then
      return 0
    fi
    echo "[setup] apt занят или упал, повтор $i/30"
    sleep 10
  done
  return 1
}

# Падаем громко и сразу, если нужного инструмента нет: лучше понятная строка в логе,
# чем молчаливое зацикливание на несуществующей команде.
need() {
  for cmd in "$@"; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      echo "[setup] ОШИБКА: не установлен ${cmd} — дальше идти бессмысленно"
      exit 1
    fi
  done
}

# Имя CLI из huggingface_hub: в свежих версиях это `hf`, в старых — `huggingface-cli`
hf_cli() {
  if command -v hf >/dev/null 2>&1; then echo hf
  elif command -v huggingface-cli >/dev/null 2>&1; then echo huggingface-cli
  else echo ""; fi
}

# Скачивание с ограниченным числом попыток: сеть чинится сама, отсутствующий файл — нет
hf_download() {
  local cli; cli=$(hf_cli)
  if [ -z "$cli" ]; then
    echo "[setup] ОШИБКА: huggingface_hub не установился, скачивать нечем"
    exit 1
  fi
  for i in $(seq 1 40); do
    if "$cli" download "$@"; then
      return 0
    fi
    echo "[setup] скачивание не удалось, попытка $i/40"
    sleep 15
  done
  echo "[setup] ОШИБКА: не удалось скачать веса за 40 попыток"
  exit 1
}

# Образы nvidia/cuda несут слой forward compatibility в /usr/local/cuda/compat: более новый
# libcuda для случая, когда драйвер хоста старее тулкита. Поддерживается он только на
# датацентровых картах, а на GeForce инициализация CUDA падает с
# «forward compatibility was attempted on non supported HW», и движок молча уезжает на CPU.
# Драйверы на Clore обычно свежие, поэтому слой нужно убрать — иначе 1 ток/с вместо 38.
fix_cuda_compat() {
  [ -d /usr/local/cuda/compat ] || return 0
  local drv tk
  drv=$(nvidia-smi 2>/dev/null | grep -o 'CUDA Version: *[0-9.]*' | grep -o '[0-9.]*$')
  tk=$(nvcc --version 2>/dev/null | grep -o 'release *[0-9.]*' | grep -o '[0-9.]*$')
  # в runtime-образе nvcc нет, но версия тулкита лежит в version.json — без неё сравнение
  # версий молча пропускалось и слой отключался всегда, в том числе когда он нужен
  [ -n "$tk" ] || tk=$(grep -o '"version" *: *"[0-9.]*"' /usr/local/cuda/version.json 2>/dev/null \
    | head -n1 | grep -o '[0-9][0-9.]*')
  if [ -n "$drv" ] && [ -n "$tk" ] && ! awk -v d="$drv" -v t="$tk" 'BEGIN{
        split(d,a,"."); split(t,b,".");
        exit !((a[1]>b[1]) || (a[1]==b[1] && a[2]>=b[2]))}'; then
    echo "[setup] драйвер ($drv) старее тулкита ($tk) — оставляю слой совместимости"
    return 0
  fi
  echo "[setup] отключаю /usr/local/cuda/compat: драйвер хоста и так поддерживает нужную CUDA"
  mv /usr/local/cuda/compat /usr/local/cuda/compat.disabled 2>/dev/null || true
  ldconfig 2>/dev/null || true
  export LD_LIBRARY_PATH="$(printf '%s' "${LD_LIBRARY_PATH}" \
    | tr ':' '\n' | grep -v 'cuda/compat' | paste -sd: -)"
}

install_base() {
  wait_for_entrypoint
  apt_retry update -qq || echo "[setup] apt-get update так и не прошёл, пробую ставить пакеты как есть"
  apt_retry install -y -qq "$@" || {
    echo "[setup] ОШИБКА: не удалось установить пакеты: $*"
    exit 1
  }
  # проверять сами имена пакетов через need нельзя: build-essential, libcurl4-openssl-dev
  # и python3-pip бинарей с такими именами не дают. Нужные команды шаблон проверяет сам.
}

# Клон репозитория с повторами. Единственный сетевой шаг, у которого их не было:
# разовый обрыв с GitHub («RPC failed ... fatal: early EOF») убивал весь деплой,
# потому что следом cmake не находил исходников и скрипт выходил с ошибкой.
git_clone_retry() {
  local url="$1" dst="$2" i
  for i in 1 2 3 4 5; do
    rm -rf "$dst"
    if git clone --depth 1 "$url" "$dst"; then
      return 0
    fi
    echo "[setup] клон $url не удался, попытка $i/5"
    sleep 10
  done
  echo "[setup] ОШИБКА: не удалось склонировать $url"
  return 1
}

install_hf() {
  # в huggingface_hub 2.x экстры hf_transfer больше нет (ускорение переехало в Xet),
  # поэтому ставим базовый пакет, а ускоритель — отдельно и без фатальности
  pip3 install -q --break-system-packages -U huggingface_hub \
    || pip3 install -q -U huggingface_hub \
    || { echo "[setup] ОШИБКА: не удалось поставить huggingface_hub"; exit 1; }
  pip3 install -q --break-system-packages -U hf_transfer 2>/dev/null \
    || pip3 install -q -U hf_transfer 2>/dev/null \
    || echo "[setup] hf_transfer недоступен — качаем штатным загрузчиком"
}

# Прогресс деплоя наружу, без ssh. sshd на Clore поднимается не сразу (первые минуты порт
# просто сбрасывает соединение), а именно в эти минуты качаются веса — и в логе была
# тишина. Поэтому контейнер сам отдаёт стадию и объём скачанного на отдельном порту.
stage() { echo "$1" > /root/.stage 2>/dev/null; }

start_status() {
  command -v python3 >/dev/null 2>&1 || return 0
  [ -f /root/.status_up ] && return 0
  cat > /root/status.py <<'PYSTATUS'
import http.server, json, os
D = os.environ.get("MODEL_DIR") or "/root/models"
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        n = 0
        for root, _, files in os.walk(D):
            for f in files:
                try:
                    n += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        try:
            st = open("/root/.stage").read().strip()
        except OSError:
            st = "setup"
        body = json.dumps({"stage": st, "bytes": n}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

http.server.HTTPServer(("0.0.0.0", int(os.environ.get("STATUS_PORT") or 5001)),
                       H).serve_forever()
PYSTATUS
  MODEL_DIR="${MODEL_DIR}" STATUS_PORT="__STATUS_PORT__" \
    setsid nohup python3 /root/status.py >/dev/null 2>&1 &
  touch /root/.status_up
}
