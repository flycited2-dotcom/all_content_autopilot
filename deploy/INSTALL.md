# Установка оркестратора (этап 2)

Сервер: `ssh sprintbox-itp` (212.116.115.150, порт 2222). Каждый шаг на сервере — только с подтверждения владельца.
Секреты (токен бота, `ORCH_API_TOKEN`) в этот файл и в git не пишутся.

## 0. Владелец
- @BotFather → `/newbot` → имя «СплитХаб · оркестратор», username на выбор. Закрепить чат с ботом.

## 1. content-factory: выкладка ветки с `agent_cli`
1. Сверка сервера с веткой (сервер бывал впереди git):
   снять снимок `/opt/content-factory/src` (как в этапе 0) и `python tools/tree_diff.py <снимок>/src <content-factory>/src`.
   Отличаться должны только `agent_cli.py` (новый) и известное `bot/control_menu.py`. Иное — стоп, разобраться.
2. Бэкап: `ssh sprintbox-itp 'cd /opt/content-factory && tar -czf backups/pre-stage2-$(date +%F).tgz src deploy config'`.
3. Выкладка `src` по `content-factory/deploy/DEPLOY.md` §1 (tar через stdin; `state/`, `.env` не трогаем).
4. `ssh sprintbox-itp 'systemctl restart cf-bot'`.
5. Проверка (именно с `PYTHONPATH=src`, пакет не установлен):
   `ssh sprintbox-itp 'cd /opt/content-factory && PYTHONPATH=src .venv/bin/python -m content_factory.agent_cli hint'` → JSON с брендами.
6. Откат: распаковать бэкап, `systemctl restart cf-bot`.

## 2. Сервис оркестратора
```bash
ssh sprintbox-itp 'apt-get install -y ffmpeg python3.12-venv && mkdir -p /opt/agents-my/state'
```
Код — tar через stdin (`orchestrator_bot/`, `requirements-vps.txt`, `deploy/`), затем:
```bash
ssh sprintbox-itp 'cd /opt/agents-my && python3 -m venv .venv && .venv/bin/pip install -r requirements-vps.txt'
```
Прогрев модели (≈ 460 МБ один раз, иначе первая голосовая скачивает её внутри бота):
```bash
ssh sprintbox-itp 'cd /opt/agents-my && .venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel(\"small\", device=\"cpu\", compute_type=\"int8\")"'
```
`.env` (права 600): `ORCH_OWNER_CHAT_ID=1264067528`, `ORCH_API_TOKEN=` из `openssl rand -hex 24` на сервере.
Токен бота владелец вводит сам:
```bash
ssh -t sprintbox-itp 'read -rsp "Токен бота: " T && echo && echo "ORCH_BOT_TOKEN=$T" >> /opt/agents-my/.env && chmod 600 /opt/agents-my/.env'
```

## 3. Ключ туннеля (ПК → VPS, только проброс порта 8790)
На ПК: `ssh-keygen -t ed25519 -f ~/.ssh/id_agents_tunnel -N "" -C agents-tunnel`.
На сервер в `/root/.ssh/authorized_keys`:
`restrict,port-forwarding,permitopen="127.0.0.1:8790",command="/bin/false" <pubkey>`.
Проверка: `ssh -i ~/.ssh/id_agents_tunnel -p 2222 root@212.116.115.150 true` — без shell; `-N -L 8790:127.0.0.1:8790` — держит туннель.

## 4. Юниты
```bash
ssh sprintbox-itp 'cp /opt/agents-my/deploy/orchestrator-*.service /opt/agents-my/deploy/orchestrator-report.timer /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now orchestrator-bot.service orchestrator-report.timer && journalctl -u orchestrator-bot -n 30 --no-pager'
```

## 5. ПК
- `pip install -r requirements-pc.txt`.
- Пользовательская переменная Windows `ORCH_API_TOKEN` = значение из серверного `.env` (не в файлы git).
- `powershell -ExecutionPolicy Bypass -File scripts\install-listener-task.ps1`, затем `Start-ScheduledTask -TaskName 'AgentsMY Listener'`.
- Журнал: `runs\chat\listener.log`.

## 6. Приёмка с владельцем
1. «что сегодня поставлено?» → ответ без действий.
2. Голосовое «до обеда поставь 2 холодильника beko» → «🎙 Расшифровываю…» → «🎙 Услышал: …» → «✅ Поставил … [Отменить]»; позиции видны в `/excel` @Sendpr1ce_bot; «Отменить» снимает.
3. «поставь все стабилизаторы» при > 40 позициях → вопрос с кнопками.
4. Остановить задачу слушателя → сообщение → «Принял, разберу, когда ПК будет на связи»; запустить → обработано.
5. «сделай один экспертный пост для ВК про тепловые насосы» → чат отвечает дальше; через 15–20 мин итог и черновик; голосовой ответ на черновик → новая версия.
6. 21:00 — сводка.
