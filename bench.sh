#!/bin/bash
# usage: bash bench.sh  -> скорость генерации на развёрнутой модели (данные берутся из .env)
cd "$(dirname "$0")"
K=$(grep -E '^API_KEY=' .env | cut -d= -f2-)
URL=$(grep -E '^API_URL=' .env | cut -d= -f2-)
MODEL=$(grep -E '^MODEL_NAME=' .env | cut -d= -f2-)
MODEL=${MODEL:-uncensored}
if [ -z "$K" ] || [ -z "$URL" ]; then echo "в .env нужны API_KEY и API_URL (разверните модель через ui.bat)"; exit 1; fi
curl -s -m 600 "$URL/chat/completions" -H "Authorization: Bearer $K" -H "Content-Type: application/json" \
 -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Write a Python function that parses a .env file into a dict. Code only, no explanation.\"}],\"max_tokens\":4096}" \
 | python -c "
import sys, json
u = json.load(sys.stdin).get('usage', {})
tps = u.get('completion_tokens_per_sec')
print('gen', u.get('completion_tokens'), 'tok', end=' ')
print(f\"{tps:.1f} tok/s\" if tps else '(движок не отдаёт tok/s)', end=' ')
print('| prompt', u.get('prompt_tokens'), 'tok')
"
