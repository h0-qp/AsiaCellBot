#!/bin/bash
# مشغّل بوت Asiacell مع إعادة تشغيل تلقائية
cd "$(dirname "$0")"
export PYTHONUNBUFFERED=1
[ -f .env ] && set -a && . ./.env && set +a
while true; do
  ./venv/bin/python bot.py >> bot.log 2>&1
  echo "[run.sh] bot exited ($?) — restarting in 5s" >> bot.log
  sleep 5
done
