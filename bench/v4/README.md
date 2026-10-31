# bench/v4 — приёмка настоящего v4-harness (lane E, независимая проверка)

Этот каталог — **не продукт**: инструмент независимого verifier'а. Он собирает тот же wiring,
что job `AGENT` в продакшене (`botai.agent.v4.factory.build_agent_wiring`: провайдер из
settings, `LocalExecutor`/`ContainerExecutor`, полный браузер, secret broker, набор
инструментов, runtime, трассы), гоняет его на настоящем DeepSeek и настоящем Chromium и
пишет санитизированное свидетельство. Продуктовый код не меняется.

Отличия от продакшена — осознанные и записаны в evidence:

* ответы «владельца» приходят из test-only ask-канала (macOS Keychain через
  `bench.live.secret_broker`), а не из PWA; модель по-прежнему видит только `sec_…`-handle,
  значения уходят в Vault/broker и не попадают в messages/trace/repo;
* trace store — in-memory (нужен только чтобы не трогать общую dev-БД); `services.db` —
  намеренно отключённая фабрика сессий: `source_*`/`rag_*` в этих сценариях не исполняются и
  отвечают честной ошибкой инструмента (никаких записей в общую БД);
* bench-канал на plain-text вопрос о кредах отвечает нуджем «запроси через secret_request» —
  это поведение test-owner'а (в продукте владелец сам решает), оно защищает от выдуманного
  username и от попадания значения в чат.

## Команды

```bash
cd /Users/yuzuru/Projects/Botai

# предпосылки, ничего не открывает и не тратит
backend/.venv/bin/python -m bench.v4.run_agent --scenario live-hse --dry-run
backend/.venv/bin/python -m bench.v4.run_agent --scenario l2-fakeuni --dry-run

# L3: живой HSE-кабинет, Keychain-креды, DeepSeek, Chromium (тратит деньги, нужен BOTAI_LIVE_LLM=1)
BOTAI_LIVE_LLM=1 backend/.venv/bin/python -m bench.v4.run_agent --scenario live-hse \
    --max-steps 60 --max-seconds 540 --max-usd 0.50

# L2: loopback fakeuni, mock-провайдер, $0
backend/.venv/bin/python -m bench.v4.run_agent --scenario l2-fakeuni
```

`--inject-task-message` — **workaround**, не product-path: кладёт задачу явным user-сообщением
в `RunTask.messages` (обход исторического бага «task не попадает в диалог»). Использовать
только чтобы отделить продуктовый баг от возможностей harness; в evidence помечается как
workaround.

## Что измеряется

`docs/v4/_evidence/2026-10-31-v4-agent/<scenario>-<runId>.json` (санитизировано) плюс
`README.md` с RU-сводкой и `phase-0.5-audit.md`:

* `status`/`outcome`/`barrier` (статус, ошибка, лимит, последние ошибки инструментов);
* `metrics`: steps, toolCalls, totalTokens, costUsd, modelCalls, длительность, лимиты;
* `tools`: последовательность инструментов с ok/error/длительностью (аргументы — без query/PII);
* `endpoints`: host+path наблюдённых URL (query и id вырезаны);
* `workspace`: файлы агента — путь, байты, sha256, формат, число записей, `scheduleLike`;
* `secretFlow`: сколько Keychain-значений выдано и как, `traceLeak`/`messagesLeak`;
* `checks.secretSweep`: сколько raw-файлов пришлось вычистить (цель — 0);
* `securityGate` (если сработал): почему прогон не принят, даже если `status=answer`;
* `liveSignals`: подтверждённые факты живого сайта (вход, страница расписания, сеть, endpoint);
* `codeState`: HEAD + предупреждение, что worktree мог содержать незакоммиченные изменения lane.

Сырые артефакты (трасса, messages, workspace агента, скачанные данные) — **вне репозитория**:
`~/.local/state/botai-v4-bench/<дата>/<run>/raw/v4-agent/` (0700, файлы 0600). Значения
первичных секретов вычищаются и там (финальный sweep).

## Приёмка

Успех = `status=answer` **и** есть артефакт/ответ **и** `messagesLeak=false` и
`traceLeak=false`. `partial` = данные получены и сохранены, ответа нет. `step_limit` и прочие
барьеры — честный результат, а не «почти успех». Один живой прогон одного SHA не доказывает
качество агента (D-010).

Сценарии живого вуза: сейчас только HSE (Keychain `botai.live.hse.*`); ЦУ первичных кредов не
имеет и в scope lane E не входит.
