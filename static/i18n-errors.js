'use strict';
// Сообщения сервера (ошибки API, итог деплоя и локального запуска, карточки «Мой ПК»).
// Лог деплоя не переводится — здесь только то, что показывается тостами и в карточках.
window.I18N_EN = window.I18N_EN || { exact: {}, patterns: [] };
// блок: свои имена не попадают в глобальную область (i18n.js объявляет const I18N_EN)
{
const I18N_EN = window.I18N_EN;
Object.assign(I18N_EN.exact, {
  // ---------- app.py ----------
  'не выбрана модель': 'no model selected',
  'неизвестный режим изоляции': 'unknown isolation mode',
  'деплой уже идёт': 'a deploy is already running',
  'уже идёт установка — дождитесь или остановите её': 'an installation is already running — wait for it or stop it',
  'значение': 'value',
  'цена': 'price',
  'диск': 'disk',
  'сеть': 'network',
  'сначала запустите модель в обычном режиме — правило ставится на её llama-server': 'start the model in normal mode first — the rule is applied to its llama-server',
  'на этом ПК модель не запущена': 'no model is running on this PC',
  'нет развёрнутой модели — сначала разверните её': 'no deployed model — deploy one first',
  'слишком большая или подозрительная тема': 'the theme is too large or looks suspicious',
  'не задан API-ключ Clore.ai (вкладка «Настройки»)': 'Clore.ai API key is not set (“Settings” tab)',
  'не задан ключ вашего API (вкладка «Настройки»)': 'your API key is not set (“Settings” tab)',
  'деплой не идёт': 'no deploy is running',
  'не указан заказ': 'no order specified',
  'нет API_URL': 'no API_URL',
  'у заказа ещё нет проброшенного порта API': 'the order has no forwarded API port yet',
  'на своём ПК запускаются только GGUF-сборки': 'only GGUF builds can run on your own PC',
  'этот квант на этот ПК не влезает — выберите другой': 'this quant does not fit on this PC — choose another one',
  'идёт установка — удалять модели можно после неё (или остановите её кнопкой «Остановить»)': 'an installation is running — models can be deleted after it finishes (or stop it with “Stop”)',
  'деплой уже идёт — дождитесь его или нажмите «Остановить деплой»': 'a deploy is already running — wait for it or press “Stop deploy”',
  'идёт деплой — переключаться на другой заказ можно после него': 'a deploy is running — you can switch to another order after it finishes',
  'пусто': 'empty',
  'запрос с другого сайта': 'request from another site',
  'ожидался Content-Type: application/json': 'expected Content-Type: application/json',
  'неизвестный эндпоинт': 'unknown endpoint',
  'ожидался JSON': 'expected JSON',

  // ---------- clore.py: _ERRORS, _DETAILS и CloreError ----------
  'общая ошибка API': 'general API error',
  'неверный API-ключ (проверьте CLORE_API_KEY)': 'invalid API key (check CLORE_API_KEY)',
  'недостаточно средств на балансе Clore': 'insufficient funds on the Clore balance',
  'сервер недоступен или уже арендован': 'the server is unavailable or already rented',
  'превышен лимит частоты запросов': 'request rate limit exceeded',
  'некорректное значение поля запроса': 'invalid request field value',
  'на балансе не хватает средств: Clore списывает оплату за сутки вперёд, а не по часам': 'not enough funds on the balance: Clore charges a full day in advance, not by the hour',
  'сервер уже арендован кем-то другим': 'the server has already been rented by someone else',
  'docker-образ не найден или недоступен': 'docker image not found or unavailable',
  'ключ не задан': 'key is not set',
  'деплой остановлен': 'deploy stopped',
  'ответ Clore не разобрать': 'cannot parse the Clore response',
  'HTTP 429 — Clore просит реже; попробуйте через минуту': 'HTTP 429 — Clore asks for fewer requests; try again in a minute',

  // ---------- deploy.py ----------
  'в .env нет API_KEY — задайте его в UI (вкладка «Настройки») или вручную': 'API_KEY is missing in .env — set it in the UI (“Settings” tab) or manually',
  'деплой остановлен: заказ отменён вручную': 'deploy stopped: the order was cancelled manually',
  'деплой остановлен вручную': 'deploy stopped manually',
  'не выбрана модель: укажите её в UI или передайте --model owner/name': 'no model selected: choose one in the UI or pass --model owner/name',
  'SSH-пароль: от 8 до 64 символов, только латинские буквы, цифры и _ . : @ = + -': 'SSH password: 8 to 64 characters, only Latin letters, digits and _ . : @ = + -',
  'в .env нет CLORE_API_KEY — задайте его в UI (ui.bat) или вручную': 'CLORE_API_KEY is missing in .env — set it in the UI (ui.bat) or manually',
  'репозиторий закрытый (gated), а HF_TOKEN не задан — веса не скачаются ни на одном сервере. Впишите токен на вкладке «Настройки»': 'the repository is gated and HF_TOKEN is not set — the weights will not download on any server. Enter the token on the “Settings” tab',
  'Clore отвечает HTTP 500 на создание заказа — проблема не в сервере. Чаще всего это слишком длинная команда заказа': 'Clore answers HTTP 500 to order creation — the problem is not the server. Most often the order command is too long',
  '⛔ установка упала с ошибкой на двух серверах подряд — похоже, дело не в серверах. Остальные не пробую. Логи — выше': '⛔ setup failed on two servers in a row — the problem is probably not the servers. Not trying the rest. Logs are above',
  'имя репозитория': 'repository name',
  'метод квантизации из config.json': 'quantization method from config.json',
  'парсер вызовов инструментов': 'tool-call parser',
  'формат весов': 'weights format',
  'имя GGUF-файла': 'GGUF file name',
  'ключ вашего API': 'your API key',
  'Claude понимает только протокол Anthropic (/v1/messages), а TabbyAPI (модели EXL2/EXL3) его не отдаёт. Подойдёт GGUF или safetensors-модель': 'Claude only speaks the Anthropic protocol (/v1/messages), and TabbyAPI (EXL2/EXL3 models) does not serve it. Use a GGUF or safetensors model',
  'сервер поднят старой версией софта и не знает имени модели для Desktop — Desktop получит ошибку 404. Разверните модель заново': 'the server was set up by an older version of the software and does not know the model name for Desktop — Desktop will get a 404 error. Deploy the model again',
  // пометки о совместимости GPU (карточки серверов)
  'разные GPU на одном хосте — движок будет равняться на слабейшую карту': 'different GPUs on one host — the engine will be limited by the weakest card',
  'vLLM начиная с движка V1 требует Ampere (CC 8.0) и новее': 'vLLM since the V1 engine requires Ampere (CC 8.0) or newer',
  'ExLlamaV3 требует Ampere (CC 8.0) и новее': 'ExLlamaV3 requires Ampere (CC 8.0) or newer',
  'llama.cpp работает, но на старых картах заметно медленнее': 'llama.cpp works, but noticeably slower on old cards',
  'нативных FP8-ядер нет, vLLM эмулирует их через Marlin — медленнее, чем на Ada и новее': 'no native FP8 kernels, vLLM emulates them via Marlin — slower than on Ada and newer',
  'MXFP4 требует Hopper (CC 9.0) и новее': 'MXFP4 requires Hopper (CC 9.0) or newer',
  'NVFP4 требует Blackwell (CC 10.0)': 'NVFP4 requires Blackwell (CC 10.0)',
  // причины, по которым фильтры скрыли серверы
  'в чёрном списке': 'blacklisted',
  'дороже заданной цены': 'above the price limit',
  'не хватает даже на час': 'balance not enough even for an hour',
  'мало VRAM': 'too little VRAM',
  'мало оперативки': 'too little RAM',
  'мало места на диске': 'too little disk space',
  'медленная сеть': 'slow network',
  'не подходит под поиск': 'does not match the search',
  'низкая надёжность хоста': 'low host reliability',
  'старая архитектура GPU': 'old GPU architecture',

  // ---------- localrun.py ----------
  'не нашёл сборок llama.cpp на GitHub': 'found no llama.cpp builds on GitHub',
  'нет свободного порта': 'no free port',
  'llama-server не ответил за 30 минут': 'llama-server did not respond within 30 minutes',
  'в архиве llama.cpp не нашлось llama-server': 'llama-server not found in the llama.cpp archive',
  'нет general.architecture — llama.cpp не поймёт, что это за модель': 'no general.architecture — llama.cpp will not know what model this is',
  'Hugging Face не отдал коммит репозитория': 'Hugging Face did not return the repository commit',
  'эта модель сейчас запущена — сначала остановите её': 'this model is running right now — stop it first',
  'такой модели нет': 'no such model',
  'CreateJobObject не сработал': 'CreateJobObject failed',
  'не удалось запустить процесс после помещения в Job Object': 'failed to start the process after placing it in the Job Object',
  'правило брандмауэра ставится только на Windows': 'the firewall rule can only be added on Windows',
  'Windows не дал прав администратора — правило не добавлено': 'Windows did not grant administrator rights — the rule was not added',
  'это не GGUF: нет сигнатуры в начале файла': 'this is not GGUF: no signature at the start of the file',
  'смещение тензора указывает за пределы данных': 'tensor offset points outside the data',
  'llama-server не установлен': 'llama-server is not installed',
  'запуск остановлен': 'launch stopped',
  'заголовок выходит за пределы файла': 'the header extends beyond the end of the file',
  'файл обрывается посреди заголовка': 'the file ends in the middle of the header',
  'скачивание остановлено': 'download stopped',
  'имя ключа метаданных — не UTF-8': 'metadata key name is not UTF-8',
  'тензор неправдоподобного размера': 'tensor of implausible size',
  'Docker не установлен': 'Docker is not installed',
  'Docker не отвечает (перезапустите Docker Desktop)': 'Docker is not responding (restart Docker Desktop)',
  'Docker установлен, но не запущен (откройте Docker Desktop)': 'Docker is installed but not running (open Docker Desktop)',

  // ---------- hfmodels.py ----------
  'пустая ссылка на модель': 'empty model link',
  'репозиторий не найден, приватный или gated — проверьте ссылку и задайте HF_TOKEN в настройках': 'repository not found, private or gated — check the link and set HF_TOKEN in Settings',

  // ---------- localpc.py ----------
  'видеокарта NVIDIA есть, но nvidia-smi не отвечает — обновите драйвер, иначе llama.cpp не увидит CUDA': 'there is an NVIDIA card, but nvidia-smi does not respond — update the driver, otherwise llama.cpp will not see CUDA',
  'дискретная видеокарта не найдена — модель будет считать процессор, это медленно. Если карта есть, укажите её VRAM вручную': 'no discrete GPU found — the model will run on the CPU, which is slow. If you do have a card, enter its VRAM manually',
  'локально запускается llama.cpp — нужна GGUF-сборка этой модели': 'local runs use llama.cpp — a GGUF build of this model is needed',
  'целиком в видеокарте': 'fully in VRAM',
  'быстро': 'fast',
  'эксперты в оперативке': 'experts in RAM',
  'средне: экспертов считает процессор': 'medium: the CPU computes the experts',
  'часть слоёв в оперативке': 'some layers in RAM',
  'медленно: каждый токен ждёт процессор': 'slow: every token waits for the CPU',
  'на процессоре': 'on the CPU',
  'очень медленно': 'very slow',
});

const N_ = String.raw`(\d+(?:[.,]\d+)?)`;
const rx_ = (s) => new RegExp('^' + s + '$');

I18N_EN.patterns.push(
  // ---------- clore.py: обёртка CloreError и вложенные части ----------
  // «Clore API: <сообщение> (<деталь>)» — у кода 2 в самом сообщении есть скобки
  [/^Clore API: неверный API-ключ \(проверьте CLORE_API_KEY\) \((.+)\)$/, 'Clore API: invalid API key (check CLORE_API_KEY) ($1)'],
  [/^Clore API: (.+?) \((.+)\)$/, 'Clore API: $1 ($2)'],
  [/^Clore API: (.+)$/, 'Clore API: $1'],
  [/^код ответа (-?\d+)$/, 'response code $1'],
  [/^сеть: (.+)$/, 'network: $1'],
  [/^Clore\.ai не отвечает \((.+)\)\. Похоже на временную блокировку IP за частые запросы — не нажимайте кнопки повторно, следующая попытка не раньше чем через (\d+) с$/, 'Clore.ai is not responding ($1). Looks like a temporary IP block for too many requests — do not press buttons again, the next attempt is possible in no less than $2 s'],

  // ---------- app.py ----------
  [/^«(.*)» — не число \((.+)\)$/, '“$1” is not a number ($2)'],
  [/^значение должно быть одной строкой: (.+)$/, 'the value must be a single line: $1'],
  [/^заказ (\d+) уже работает \((.+)\)\. Нужен ещё один сервер — поставьте галочку «арендовать ещё один сервер»$/, 'order $1 is already running ($2). Need another server — tick “rent one more server”'],
  [/^заказ (\d+) отменить не удалось — он продолжает оплачиваться: (.+)$/, 'order $1 could not be cancelled — it is still being billed: $2'],
  [/^заказ (\d+) не найден$/, 'order $1 not found'],
  [/^недопустимый Host: (.+)$/, 'invalid Host: $1'],
  [/^сервер не сообщил, какая на нём модель \(заказ старой версии\) — контекст взят у движка: (\d+)$/, 'the server did not report which model it runs (order from an older version) — context taken from the engine: $1'],
  [/^сервер не сообщил, какая на нём модель \(заказ старой версии\) — контекст по умолчанию: (\d+)$/, 'the server did not report which model it runs (order from an older version) — default context: $1'],

  // ---------- deploy.py: итоговые ошибки деплоя ----------
  [/^не удалось арендовать ни один из (\d+) серверов$/, 'failed to rent any of the $1 servers'],
  [/^от прошлой параллельной установки остались активные заказы \((.+?)\) — они оплачиваются\. Отмените их: python stop\.py или вкладка «Заказы»$/, 'active orders are left from the previous parallel install ($1) — they are being billed. Cancel them: python stop.py or the “Orders” tab'],
  [/^на сервере (.+?) остался заказ (.+?) от прерванного деплоя — он оплачивается\. Отмените его: python stop\.py или вкладка «Заказы»$/, 'an order $2 from an interrupted deploy is left on server $1 — it is being billed. Cancel it: python stop.py or the “Orders” tab'],
  [/^заказ (.+?) ещё активен и оплачивается, но модель на нём не отвечает\. Отмените его \(«Отменить заказ» или python stop\.py\) либо запустите с галочкой «арендовать ещё один сервер» \(--force\)$/, 'order $1 is still active and being billed, but the model on it does not respond. Cancel it (“Cancel order” or python stop.py) or run with “rent one more server” ticked (--force)'],
  [/^недопустимое значение — (.+?): (.+)\. Разрешены только латинские буквы, цифры и символы _ \. - \(в пути ещё \/\)\. Такое значение в скрипт сервера не подставляю$/, 'invalid value — $1: $2. Only Latin letters, digits and the characters _ . - (plus / in paths) are allowed. I will not insert such a value into the server script'],
  [/^заказ (\d+) отменён извне — очередь серверов дальше не идёт$/, 'order $1 was cancelled externally — the server queue stops here'],
  [/^заказ (\d+) не удалось отменить \((.+)\) — он продолжает оплачиваться\. Номер оставлен в \.env: отмените его кнопкой «Отменить заказ» или stop\.py\. К следующему серверу не перехожу$/, 'order $1 could not be cancelled ($2) — it is still being billed. The number is kept in .env: cancel it with “Cancel order” or stop.py. Not moving on to the next server'],
  [/^ни один из выбранных серверов недоступен: (.+?)\. Их успели арендовать или хост снял их с продажи — обновите список на вкладке «Сервер» и выберите другие$/, 'none of the selected servers is available: $1. They were rented already or the host took them off the market — refresh the list on the “Server” tab and choose others'],
  [rx_(String.raw`на Clore нет подходящих серверов: нужно ≥${N_} GiB VRAM, ≥${N_} GB диска(.*?)\. Баланс \$${N_}: оплата идёт по часам, поэтому отсеяны только серверы дороже \$${N_}/ч — на них не хватит и на час`),
    'no suitable servers on Clore: need ≥$1 GiB VRAM, ≥$2 GB disk$3. Balance $$4: billing is hourly, so only servers above $$5/h are filtered out — the balance would not cover even an hour on them'],
  // необязательные вставки внутри «нет подходящих серверов» — переводятся по цепочке
  [rx_(String.raw`, цена ≤ \$${N_}/ч(.*)`), ', price ≤ $$1/h$2'],
  [/^, GPU содержит «(.+?)» — снимите фильтр, если таких машин нет(.*)$/, ', GPU contains “$1” — remove the filter if there are no such machines$2'],
  [/^\. Карты, на которых vLLM не запускается \(старее Ampere\), отсеяны$/, '. Cards vLLM does not run on (older than Ampere) are filtered out'],
  [/^заказ на сервер (.+?) мог быть создан, но найти его в списке заказов не удалось\. Он может тарифицироваться: проверьте вкладку «Заказы» или запустите python stop\.py — он найдёт заказ по номеру сервера$/, 'an order for server $1 may have been created, but it could not be found in the order list. It may be billed: check the “Orders” tab or run python stop.py — it will find the order by server number'],
  [/^⛔ модель скачалась, но движок не запустился на сервере (.+?) — похоже, дело в модели или контексте, а не в сервере\. Остальные серверы очереди не пробую: там было бы то же самое\. Логи — выше$/, '⛔ the model downloaded, but the engine did not start on server $1 — the problem seems to be the model or the context, not the server. Not trying the other servers in the queue: the result would be the same. Logs are above'],
  [/^ни на одном из (\d+) серверов модель не поднялась$/, 'the model did not come up on any of the $1 servers'],
  [/^(.+?): недопустимые символы — не стану вписывать это в скрипт$/, '$1: invalid characters — I will not write this into the script'],
  [/^не могу прочитать (.+) — поправьте или удалите его в окне настройки Claude Desktop$/, 'cannot read $1 — fix or delete it in the Claude Desktop settings window'],
  [/^(.+): неожиданный формат — не трогаю$/, '$1: unexpected format — leaving it untouched'],
  // предупреждения Claude Code / Claude Desktop
  [/^Claude Code не найден в PATH — поставьте его \(npm i -g @anthropic-ai\/claude-code\), а каталог (.+) добавьте в PATH$/, 'Claude Code not found in PATH — install it (npm i -g @anthropic-ai/claude-code) and add the folder $1 to PATH'],
  [/^контекст (\d+) маловат: одни инструкции Claude Code занимают ~20 тыс\. токенов\. Нужно хотя бы (\d+)$/, 'context $1 is too small: Claude Code instructions alone take ~20k tokens. At least $2 is needed'],
  [/^есть политика администратора (.+) — Desktop возьмёт настройки оттуда, а не наши$/, 'there is an administrator policy $1 — Desktop will take its settings from there, not ours'],
  [/^до этого была выбрана конфигурация «(.+)» — она сохранена, вернуть её можно в окне Configure Third-Party Inference$/, 'the configuration “$1” was selected before — it is kept, you can switch back to it in the Configure Third-Party Inference window'],
  // пометки о совместимости GPU: «<архитектура>: <причина>»
  [/^([A-Za-z][\w +-]*): (vLLM начиная с движка V1 требует Ampere \(CC 8\.0\) и новее|ExLlamaV3 требует Ampere \(CC 8\.0\) и новее|llama\.cpp работает, но на старых картах заметно медленнее|нативных FP8-ядер нет, vLLM эмулирует их через Marlin — медленнее, чем на Ada и новее|MXFP4 требует Hopper \(CC 9\.0\) и новее|NVFP4 требует Blackwell \(CC 10\.0\))$/, '$1: $2'],
  [/^([A-Za-z][\w +-]*) — старая архитектура, скорость будет заметно ниже$/, '$1 — old architecture, speed will be noticeably lower'],
  // счётчики «Скрыто фильтрами: <b>3</b> мало VRAM · …»
  [/^(в чёрном списке|дороже заданной цены|не хватает даже на час|мало VRAM|мало оперативки|мало места на диске|медленная сеть|не подходит под поиск|низкая надёжность хоста|старая архитектура GPU)( ·|\.)$/, '$1$2'],

  // ---------- localrun.py: LocalError / GGUFError ----------
  [/^(.+): размер (\d+) вместо (\d+) — скачано не целиком$/, '$1: size $2 instead of $3 — not fully downloaded'],
  [/^(.+): SHA-256 не совпал \(ожидался (.+?)…, получен (.+?)…\) — файл удалён, запускать его нельзя$/, '$1: SHA-256 mismatch (expected $2…, got $3…) — the file was deleted, it must not be run'],
  [/^(.+): связь обрывается \((.+)\)\. Запустите ещё раз — скачанное сохранено, докачка продолжится$/, '$1: the connection keeps dropping ($2). Run it again — what was downloaded is kept, the download will resume'],
  [/^(.+): сеть недоступна \((.*)\)$/, '$1: network unavailable ($2)'],
  [/^в репозитории нет файла (.+)$/, 'the repository has no file $1'],
  [rx_(String.raw`на диске ${N_} ГиБ свободно, а нужно ${N_} ГиБ — освободите место или укажите другой каталог \(LOCAL_DIR\)`), '$1 GiB free on disk, but $2 GiB is needed — free up space or choose another folder (LOCAL_DIR)'],
  [/^не удалось понизить уровень целостности: (.*)$/, 'failed to lower the integrity level: $1'],
  [/^ограничения Job Object не применились \((-?\d+)\)$/, 'Job Object limits were not applied ($1)'],
  [/^процесс не помещён в Job Object \((-?\d+)\)$/, 'the process was not placed in the Job Object ($1)'],
  [/^не получил digest образа (.+)$/, 'did not get the image digest for $1'],
  [/^в сборке (.+?) нет архива (.+)$/, 'release $1 has no archive $2'],
  [/^у (.+) нет SHA-256 в GitHub — без проверки ставить не буду$/, '$1 has no SHA-256 on GitHub — I will not install it unverified'],
  [/^у (.+) нет SHA-256 в Hugging Face — без проверки качать не буду$/, '$1 has no SHA-256 on Hugging Face — I will not download it unverified'],
  [/^архив пытается писать за пределы каталога: (.+)$/, 'the archive tries to write outside the folder: $1'],
  [/^docker pull не удался: (.*)$/, 'docker pull failed: $1'],
  [/^docker недоступен: (.+)$/, 'docker unavailable: $1'],
  [/^контейнер не запустился: (.*)$/, 'the container did not start: $1'],
  [/^llama-server завершился при запуске\. Последние строки лога: ?(.*)$/, 'llama-server exited on startup. Last log lines: $1'],
  [/^неизвестная версия GGUF: (.+)$/, 'unknown GGUF version: $1'],
  [/^неправдоподобно много записей: (\d+) тензоров, (\d+) ключей$/, 'implausibly many entries: $1 tensors, $2 keys'],
  [/^неправдоподобное выравнивание: (.+)$/, 'implausible alignment: $1'],
  [/^строка длиной (\d+) байт — у настоящих моделей таких нет$/, 'a string $1 bytes long — real models have no such strings'],
  [/^тензор с (\d+) измерениями$/, 'tensor with $1 dimensions'],
  [/^неизвестный тип тензора (.+)$/, 'unknown tensor type $1'],
  [/^измерение тензора (.+)$/, 'tensor dimension $1'],
  [/^ключ (.+?): неизвестный тип значения (.+)$/, 'key $1: unknown value type $2'],
  [/^массив (.+) из (\d+) элементов$/, 'array $1 of $2 elements'],
  [/^массив (.+) с вложенным типом (.+)$/, 'array $1 with nested type $2'],

  // ---------- hfmodels.py ----------
  [/^ожидался формат owner\/name, получено: (.+)$/, 'expected owner/name format, got: $1'],
  [/^HuggingFace не отдал список файлов (.+) — повторите через минуту$/, 'Hugging Face did not return the file list of $1 — try again in a minute'],
  [/^Hugging Face недоступен: (.+)$/, 'Hugging Face is unavailable: $1'],
  [/^не похоже на репозиторий модели: (.+)$/, 'does not look like a model repository: $1'],

  // ---------- localpc.py: карточки «Мой ПК» ----------
  [/^(.+) \(общая память\)$/, '$1 (unified memory)'],
  [rx_(String.raw`не влезет даже самый лёгкий квант \((.+?), ${N_} ГиБ\): на этом ПК для модели есть ${N_} ГиБ VRAM и ${N_} ГиБ RAM`), 'even the lightest quant will not fit ($1, $2 GiB): this PC has $3 GiB VRAM and $4 GiB RAM available for the model'],
  [rx_(String.raw`не влезет даже самый лёгкий квант: на этом ПК для модели есть ${N_} ГиБ VRAM и ${N_} ГиБ RAM`), 'even the lightest quant will not fit: this PC has $1 GiB VRAM and $2 GiB RAM available for the model'],
  [/^(.+?): (.+?), контекст (\d+)k — (.+) \(на (\d+)k не хватает памяти\)$/, '$1: $2, context $3k — $4 (not enough memory for $5k)'],
  [/^(.+?): (.+?), контекст (\d+)k — (.+)$/, '$1: $2, context $3k — $4'],

  // предупреждения выводятся одной строкой с «⚠ » впереди
  [/^⚠ (.+)$/, '⚠ $1'],
);
}
