# Research: Toolathlon-GYM как источник для бенчмарка (SPA-37 → E-23 / E-09)

Дата: 2026-06-10 · Спайк по [SPA-37](https://linear.app/spawnhive/issue/SPA-37). Изучен код
[eigent-ai/toolathlon_gym](https://github.com/eigent-ai/toolathlon_gym) (clone, commit на дату спайка),
сверен с нашим стеком (`agent-image/agent.py`, SPA-41 Registry, Benchmark Case Store из `docs/benchmarks.md`).

## Решение (TL;DR)

- **E-23 (внешний набор задач): ДА, брать** — через пилот на 10–20 задачах перед массовым импортом.
  Лицензия Apache 2.0, всё локально, eval-скрипты автономны и добротны, их 25 MCP-серверов ложатся в
  наш Registry (SPA-41) почти 1-в-1.
- **E-09 (источник канонических траекторий): НЕТ** — траекторий в данных нет. `task_config.json.meta`
  пуст у всех 503 задач; есть только *набор* серверов (`needed_mcp_servers`), без последовательности
  вызовов. Набор можно использовать как слабый сигнал уровня E-13 (`required_tools`, match=all), а
  канонические траектории при желании майнить позже из собственных эталонных прогонов.

## Что это (проверенные факты)

503 задачи в `tasks/finalpool/`, 25 stdio MCP-серверов, всё работает локально от одного PostgreSQL
(`db/init.sql.gz`, 8.2 MB → 11 схем: `canvas`, `sf` (Snowflake-мок), `email`, `gcal`, `gform`,
`gsheet`, `notion`, `woocommerce`, `arxiv`, `scholarly`, `train`). Никаких внешних API и реальных
токенов в рантайме (`configs/token_key_session.py` — заглушка). Построен на инфраструктуре
Toolathlon (HKUST-NLP); расширение пула — eigent-ai (CAMEL).

Распределение по числу MCP на задачу: 4 → 123, 5 → 133, 6 → 105, 7 → 126, 8 → 16.
Семейства: snowflake/HR — 85, woocommerce — 63, terminal — 56, canvas/LMS — 55, yahoo-finance — 47,
fetch — 35, youtube — 25, howtocook — 24, playwright — 31, arxiv/scholarly — 24, прочие — мельче.

### Анатомия задачи

```
<task>/
├── task_config.json        # needed_mcp_servers (4–8), needed_local_tools, meta (везде пуст)
├── docs/task.md            # описание для агента; имена сервисов обфусцированы (анти-shortcut)
├── docs/agent_system_prompt.md
├── preprocess/main.py      # сброс/посев состояния: DELETE по схемам + INSERT тестовых данных (psycopg2 — в 422/503)
├── evaluation/main.py      # детерминированная проверка (exit 0/1)
├── initial_workspace/      # входные файлы агента (md/pdf/json/xlsx/csv/py…)
└── groundtruth_workspace/  # эталонные артефакты (743 файла; у 14 задач нет — eval только по БД)
```

Пайплайн: `preprocess` → агент (CAMEL ChatAgent, бюджет 100 шагов, завершение через `claim_done`)
→ `evaluation/main.py --agent_workspace … --groundtruth_workspace … --launch_time … [--res_log_file …]`.

### Качество eval (спот-чек)

Скрипты автономные (psycopg2 + openpyxl/python-docx), медиана ~213 строк, гранулярные `[PASS]/[FAIL]`
проверки. Важно: ожидания часто **вычисляются из живой БД**, а не захардкожены (например,
`canvas-at-risk-intervention` сверяет строку отчёта с `SELECT COUNT(*)` по той же схеме) — устойчиво
и честно. Допуски разумные (числовые tolerance, регистронезависимые заголовки). 409/503 eval-скриптов
ходят в PostgreSQL — eval должен запускаться в окружении с доступом к БД задачи.

## Лицензия

**Apache 2.0** (LICENSE в корне) — использование, модификация и редистрибуция разрешены с указанием
авторства. Нюанс для *публичной* перепубликации (поздний этап E-23): данные мока производны от Kaggle
OULAD / HR Analytics / Yahoo Finance / Amazon+DummyJSON — при перепубликации датасета сохранять
attribution и проверить условия первоисточников. Для внутреннего бенчмаркинга ограничений нет.

## Совместимость с нашим стеком

| Их сторона | Наша сторона | Вердикт |
|---|---|---|
| MCP-конфиги `configs/mcp_servers/*.yaml`: stdio, `{command, args, env, cwd}` + переменные `${local_servers_paths}`, `${agent_workspace}`, `${task_dir}` | Registry (SPA-41): kind=mcp, `config={command,args}`, `secrets=env`; агент читает `MCP_SERVERS=[{name,command,args,env}]` и сам поднимает stdio (`agent-image/agent.py:_connect_mcp_servers`) | **Почти 1-в-1.** Не хватает: (а) проброса `cwd` в `StdioServerParameters` (поле в mcp-SDK есть — патч на пару строк); (б) резолва их шаблонных переменных на этапе импорта/спавна |
| Окружение: образ ubuntu22 + uv + node22 + playwright + 19 пребилженных серверов в `/opt/local_servers` | Наш `agent-image` — python-slim, только agent.py | **Производный образ**: `FROM toolathlon-pack:latest` + наши `agent.py/entrypoint.py/…` + `pip install litellm mcp fastapi uvicorn httpx` в их venv. Дешевле, чем тащить node/uv/серверы в наш образ |
| БД: `toolathlon_pg` (postgres:15 из `init.sql.gz`), MCP-серверы и eval ходят в неё по `PG*` env | `docker-compose.yml`, своя сеть агентов | Отдельный сервис/profile в compose + общая сеть + `PG*` env в контейнер агента |
| Изоляция: `run_parallel.sh` даёт **каждой задаче свой postgres + сеть** (а `run_containerized.sh` сериализует прогоны flock-ом из-за общей БД) | Наш раннер сам управляет контейнерами | Перенять схему per-run postgres — иначе параллельные прогоны делят состояние |
| Агентный цикл: CAMEL ChatAgent | Свой LLM-цикл в agent.py | **Их агент не нужен.** `preprocess` и `evaluation` — самостоятельные shell-скрипты; наш агент встаёт между ними без адаптации их кода. Их гейт «eval только после claim_done» нам не нужен — гоняем eval безусловно |

### Маппинг формата на Benchmark Case Store (pre-E-23)

`input.title/description` ← `docs/task.md` (обфусцированный текст — это плюс);
`meta` ← `needed_mcp_servers`, max_steps, семейство; `gold.capability_spec.required_tools` ←
`needed_mcp_servers` (слабый set-level сигнал). **Гэп формата**: их «золото» — это *исполняемый
чекер + эталонные артефакты*, у нашего `CaseGold` такого слота нет. Нужно расширение формата кейса
(уровень E-23): `gold.external_eval = {preprocess_command, eval_command, groundtruth_path}` +
блок окружения (ссылки на registry `tool_ids`, требование `toolathlon_pg`). Само хранилище данных
не вендорим в git — клон репозитория остаётся внешней зависимостью (путь конфигурируется).

### Пригодность для E-11 / E-12

**Да.** Окружение детерминированное: фиксированный дамп БД, `preprocess` приводит состояние к
эталону перед каждым прогоном, per-run изоляция воспроизводима — одна задача гоняется N раз
(variance, E-11). Perturbation-инъекция (E-12, `AGENT_TOOL_INJECTION`) работает на нашем уровне
и от датасета не зависит.

## Объём интеграции (декомпозиция)

| # | Подзадача | Размер |
|---|---|---|
| 1 | **Инфра**: compose-profile `toolathlon` (`toolathlon_pg` + сборка образов), общая сеть с агентами | S |
| 2 | **agent.py**: проброс `cwd` в StdioServerParameters + `PG*` env passthrough | XS |
| 3 | **Импорт MCP в Registry**: скрипт/CLI `configs/mcp_servers/*.yaml` → 25 registry-entries (kind=mcp) с резолвом шаблонных переменных | S |
| 4 | **Адаптер кейсов**: `tasks/finalpool/<task>` → `backend/benchmarks/toolathlon/*.yaml`; расширение формата `gold.external_eval` в `quality/benchmark.py` | M |
| 5 | **Раннер-клей**: preprocess → прямой спавн нашего агента (= benchmark execution path из SPA-40, мимо оркестратора) → eval → бинарный вердикт в `quality_records` + E-20 snapshot | M |
| 6 | **Пилот**: 10–20 задач из разных семейств; сравнение их pass/fail с нашими профилями E-02/E-07 (agreement в духе E-17); go/no-go на массовый импорт | S–M |

Итого до пилота: ~2 коротких задачи (1–3) + 2 средних (4–5). Пункт 5 пересекается со SPA-40 — прямой
спавн стоит делать как общий механизм, а не отдельно для Toolathlon.

## Риски

- **Качество сгенерированного пула**: 503 задачи — масштабированное расширение оригинального
  Toolathlon; спот-чеки eval хорошие, но до массового импорта обязателен пилот (п. 6) с ручной
  верификацией выборки.
- **Тяжёлый образ**: ubuntu + node + uv + chromium + пребилд 19 серверов — длинная сборка и
  несколько ГБ диска. Локально приемлемо; в CI собирать по profile.
- **Eval требует живую БД** (409/503) — eval гонять в контейнере, подключённом к postgres этого
  прогона, и передавать тот же `--launch_time`, что и в preprocess (иначе ложные FAIL по датам).
- **Параллелизм только с per-run postgres** — общая БД допускает лишь сериализованные прогоны.
- **Перепубликация данных** (поздний E-23) — проверить лицензии первоисточников Kaggle; для
  внутреннего использования вопрос не стоит.
