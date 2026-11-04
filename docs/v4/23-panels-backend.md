# 23. Панели «Учёбы»: серверная часть (D-039)

Статус: **реализовано в ветке PR 3** («S4 panels backend»), 2026-11-04. Решение — D-039 в
[06-decisions.md](06-decisions.md), план — [22-self-expanding.md](22-self-expanding.md) §2.
UI панелей — отдельный PR (`apps/web/src/features/study/**`); здесь только то, что делает сервер.

Идея: **данные остаются в доменных таблицах** с провенансом и детерминированным sync, а панель —
тонкий конфиг представления (`panels`). Панель не хранит данных; сервер на каждый запрос
собирает ограниченные данные по её набору (`dataset`).

## 1. Новые виды данных `profile` и `ranking`

Оба вида **только v4**. Словарь v3-рецептов (`contracts/specs.py:Kind`, `recipe.schema.json`,
`sources/vocab.py:VOCAB/REQUIRED/KEY_FIELDS/KIND_LABELS`) не менялся: v3-ученик итерирует эти
таблицы, и новые виды не должны попасть в его схемы. Новое живёт рядом (`V4_*`) и сливается в
представления `ALL_*`, которые читают v4-раннер, домен-писатель и подсказка агенту
(`source_schema`). Тип `DomainKind = Kind | V4Kind`.

| Вид | Таблица | Поля playbook (camelCase) | Обязательные | Ключ канарейки |
|---|---|---|---|---|
| `profile` | `profile_fields` | `externalId, field, label, value, group` | `field, value` | `field, value` |
| `ranking` | `ranking_entries` | `externalId, board, position, label, score, scoreNum, isMe, total, period` | `board, position, label` | `board, position, label` |

Правила приведения (`sources/normalize.py`):

* `profile`: `value` — строка **как дал источник** (число станет текстом, булево — «Да»/«Нет»,
  ничего не разбирается). `label` по умолчанию — `field`. Поле, в имени или подписи которого
  пароль, токен, секрет, API-ключ, CVV/OTP, «одноразовый» — **отбрасывается и не сохраняется**
  (первичные секреты в БД не попадают, AGENTS.md п. 5).
* `ranking`: `position` — целое ≥ 1; `total` — целое ≥ 1; `isMe` — булево или «true/да/1»;
  `score` — текст источника, `score_num` — только если источник дал `scoreNum` либо `score` —
  ровно одно число («81,2»). «92 балла», «высокий» числа не дают. Чужих людей храним минимально:
  подпись и балл.
* Пустое значение и заглушки (`"None"`, `"null"`, `"nan"`, `"undefined"`, `"-"`, `"—"`, `"н/д"`)
  для оценок, подписей и баллов — это «нет значения»: запись без обязательного значения
  отбрасывается, необязательное поле становится `NULL`, а не строкой «None».

Провенанс — как у остальных доменных таблиц: `(source_id, external_id)` уникален (частичный
индекс), строки пишет `sources/v4/domain.write_records`, строки v3 (`connection_id`) никогда не
затрагиваются. Строки видны только из живых источников (`domain.sources.load`): выключенный
источник скрывает данные, не удаляя их. Дедупликация между источниками одного вуза — по
`source_priorities` (виды `profile` и `ranking` допустимы в приоритетах).

Дополнительно к `/api/v1/{courses,grades,…}`: `GET /api/v1/profile?space_id=` и
`GET /api/v1/ranking?space_id=&board=`; `GET /spaces/{id}/sections` умеет возвращать виды
`profile` и `ranking`.

## 2. Числа оценок

`grades.value_num` и `grades.scale_max` (nullable float). Заполняются при приведении
(`sources/grade_scale.py`) **только если источник напечатал однозначно и число, и верх шкалы**.
Приложение не усредняет, не пересчитывает шкалы и не угадывает; `value` и `scale` остаются
текстом источника.

| value | scale | value_num / scale_max |
|---|---|---|
| `8` | `10`, `из 10`, `10 баллов`, `10-балльная`, `шкала 10`, `0-10`, `от 0 до 10` | 8 / 10 |
| `8,5` или `8.5` | `10` | 8.5 / 10 |
| `8/10`, `8 из 10` | — | 8 / 10 |
| `86%` | — | 86 / 100 |
| `зачтено`, `отлично`, `B+` | любая | null / null |
| `8` | нет | null / null (число без шкалы) |
| `11` | `10` | null / null (больше верха шкалы) |
| `8/10` | `100` | null / null (две шкалы не совпали) |

Существующие строки остаются с `NULL` до следующего sync своего источника (миграция аддитивная,
без пересчёта).

## 3. Таблица `panels`

| Колонка | Значение |
|---|---|
| `id` | ULID |
| `user_id` | владелец, `ON DELETE CASCADE`, условие тенанта в каждом запросе |
| `space_id` | вуз, `ON DELETE CASCADE` |
| `source_id` | источник, ради которого панель создана; `SET NULL` |
| `key` | стабильный слаг ≤ 60 (`^[a-z0-9][a-z0-9:_-]*$`), уникален внутри вуза |
| `type` | `kpi \| chart \| table \| ranking \| profile \| list` (CHECK) |
| `spec` | jsonb, проверенный `panels.schemas` (camelCase) |
| `status` | `proposed \| active \| dismissed` (CHECK) |
| `visible`, `hidden_in_all` | переключатели студента; `hidden_in_all` — скрыть только в режиме «Все вузы» |
| `position` | порядок внутри вуза |
| `origin` | `system \| agent` (CHECK) |
| `proposed_by_job_id` | задание агента, `SET NULL` |
| `created_at`, `updated_at` | — |

Миграция `d5b2e7f9a1c3_study_panels` аддитивна: `panels`, `profile_fields`, `ranking_entries`,
`grades.value_num/scale_max`; `downgrade` удаляет ровно добавленное.

## 4. Spec v1 (`botai/panels/schemas.py`)

`{v: 1, type, title, icon, dataset: {kind, board?, courseId?}, view}` — pydantic-union по
`type`; неизвестные поля и значения неверного типа (`"yes"` вместо флага) отвергаются.

* `title` — ≤ 60 знаков, **простой текст**: без `<`/`>`, обратных кавычек, `://`, `www.`,
  markdown-ссылок/выделения/заголовков/списков, `javascript:`, управляющих символов.
* `icon` — только имя из реестра иконок дизайна: `grade course person user university schedule
  deadline table number group teacher today news document` и новые `chart trophy star idCard trend`
  (их добавляет `packages/design`; vitest сверяет список с реестром).
* `dataset.board` — только для рейтинга (обязателен), ≤ 120 знаков, без управляющих символов
  (это выбор строк, а не показываемый текст). `dataset.courseId` — только для оценок, ULID курса
  (`CourseOut.id`).

| `type` | Набор | `view` (по умолчанию) | Пределы |
|---|---|---|---|
| `kpi` | `grades` | `metrics`: `count`, `latest`, `best` (`count, latest`) | 1–3 без повторов |
| `chart` | `grades` | `variant` `bar\|line` (`line`), `x` `date\|course` (`date`), `limit` (24) | `limit` ≤ 50 подписей; серий ≤ 6 |
| `table` | `grades` | `columns` из `title, course, value, scale, date`; `limit` (20) | колонок ≤ 5; строк ≤ 100 |
| `ranking` | `ranking` + `board` | `top` (10), `highlightMe` (true) | `top` ≤ 50 |
| `profile` | `profile` | `fields`: порядок ключей полей (пусто — все) | ≤ 30 ключей |
| `list` | `grades` | `limit` (10) | ≤ 50 |

## 5. Сервис (`botai/panels/service.py`)

Функции принимают `user_id`, делают `flush`, не делают `commit`; чужой id — `NOT_FOUND`.

* `ensure_default_panels(session, *, user_id, space_id) -> list[str]` — идемпотентно (уникальный
  `(space_id, key)`, `INSERT … ON CONFLICT DO NOTHING`) создаёт активные `origin=system` панели
  по данным живых источников вуза: оценки → `grades-overview` (kpi) и, если среди оценок есть
  числовые, `grades-chart` (по дате, если у числовых есть даты, иначе по курсам); профиль →
  `student-profile`; каждый список рейтинга → `ranking:<слаг>-<хэш>` (ключ стабилен для
  названия списка). Существующая панель не меняется ни в каком статусе: скрытое, отклонённое и
  переставленное студентом не возвращается. Вызывается из `write_records` после записи видов
  `grades/profile/ranking` и лениво в `GET /panels`.
* `propose_panel(session, *, user_id, space_id, key, spec, source_id=None, job_id=None,
  force=False) -> Proposal` — для tool'а агента (PR 6). Проверяет ключ и spec, вуз/источник/задание
  владельца; создаёт `proposed`, `visible=false`, `origin=agent`. Идемпотентно по `key`: тот же
  ключ у ещё не принятой панели обновляет spec (`updated`), у живой — ничего не меняет
  (`exists`), у отклонённой — не предлагает снова (`dismissed`), если не `force`. Не больше
  24 непотушенных панелей на вуз.
* `set_panel_status(...)` — `proposed→active|dismissed`, `active→dismissed`, `dismissed→active`;
  принятие включает `visible`, отклонение выключает. Иной переход — `CONFLICT`.
* `update_panel(...)` — переключатели `visible`, `hidden_in_all`, `position`; отклонённую панель
  так включить нельзя (`CONFLICT`).
* `prune_after_source_removal(...)` — при удалении источника: панели с этим `source_id` и
  системные панели, данных которых больше нет у живых источников вуза.

## 6. API (`/api/v1`, только владелец, только `BOTAI_AGENT_ENGINE=v4`)

* `GET /panels?space_id=&status=active|proposed` — панели вуза или всех вузов пользователя:
  вузы по возрасту, внутри вуза по `position`. Активные включают и выключенные студентом
  (`visible: false`, `data: null`), чтобы экран мог предложить переключатель. Панель с
  испорченным сохранённым spec пропускается, а не роняет ответ. Для `status=active` системные
  панели вуза создаются здесь же, если sync ещё не успел.
* `PATCH /panels/{id}` — тело `{visible?, hiddenInAll?, position?(0…10000)}`; остальное — 422.
* `POST /panels/{id}/accept`, `POST /panels/{id}/dismiss` — для предложений агента.
* Чужие панели и вузы — 404; вне v4 — 404; без сессии — 401.

Форма панели (поля — camelCase, `null` в ответе явные):

```json
{
  "id": "01K…", "key": "grades-overview", "type": "kpi",
  "spec": {
    "v": 1, "type": "kpi", "title": "Успеваемость", "icon": "grade",
    "dataset": {"kind": "grades", "board": null, "courseId": null},
    "view": {"metrics": ["count", "latest"]}
  },
  "status": "active", "visible": true, "hiddenInAll": false, "position": 0,
  "origin": "system", "sourceId": null,
  "createdAt": "2026-11-04T10:00:00Z", "updatedAt": "2026-11-04T10:00:00Z",
  "space": {"id": "01K…", "title": "Университет", "shortTitle": "ВШЭ", "accentHue": 262.2},
  "data": { "kind": "grades", "…": "см. ниже" }
}
```

`data` — различается по `kind`; `null`, если панель выключена студентом или отклонена.

```json
// grades (kpi/chart/table/list)
{"kind": "grades", "total": 4, "numeric": 3,
 "points": [{"id": "…", "title": "Контрольная", "course": "Математический анализ",
             "courseId": "01K…", "value": "8,5", "scale": "из 10",
             "valueNum": 8.5, "scaleMax": 10, "gradedAt": "2026-10-19T07:00:00Z"}],
 "latest": {…точка…}, "best": {…точка…}}
// profile
{"kind": "profile", "total": 3,
 "fields": [{"field": "group", "label": "Группа", "value": "ПМ-31", "group": "Обучение"}]}
// ranking
{"kind": "ranking", "board": "Рейтинг потока", "total": 4, "participants": 120,
 "period": "Осень 2026",
 "rows": [{"position": 14, "label": "Иванова А.", "score": "81,2", "scoreNum": 81.2,
           "isMe": true, "total": 120, "period": "Осень 2026"}]}
```

Пределы данных задаёт `view` панели, сервер их соблюдает:

* `total` — сколько оценок/полей/мест в наборе; `numeric` — сколько оценок с числами;
* `kpi`: `points` пуст, есть `latest` (самая новая оценка набора) и `best` (наибольшее
  `valueNum` внутри **самой частой шкалы**; оценки разных шкал не сравниваются; `null`, если
  чисел нет). Средних нет нигде;
* `chart`: только числовые оценки (при `x=date` — ещё и с датой) не более чем 6 самых частых
  шкал, самые новые `limit`; при `x=date` — от старых к новым;
* `table`/`list`: самые новые `limit` оценок;
* `ranking`: первые `top` мест по `position` и, при `highlightMe`, своя строка, если она ниже;
* `profile`: поля в порядке `view.fields` (без учёта регистра) либо все (≤ 60).

## 7. Удаление и сброс

* Удаление источника (`sources/v4/deletion.py`): `DOMAIN_MODELS` теперь включает
  `ProfileField` и `RankingEntry`; панели этого источника и системные панели без данных
  удаляются, выбор студента у остальных панелей сохраняется.
* Удаление вуза (`spaces/service.delete_space`): строки v4-источников вуза удаляются явно
  (раньше внешний ключ `SET NULL` оставлял их бесхозными для всех видов), панели уходят каскадом.
* Сброс аккаунта (`ops/reset_account`): таблицы перечисляются по схеме, новые таблицы с
  `user_id` попадают сами; тест проверяет, что чужие данные не затронуты.

## 8. Проверки

`tests/panels/` (схема, сервис, API, sync через fakeuni, удаление/сброс),
`tests/sources/test_grade_scale.py`, `tests/sources/test_v4_only_kinds.py`,
`tests/db/test_study_panels_migration.py`. fakeuni: `tests/fakeuni/rating.py` — синтетические
`/api/v2/{grades,profile,ratings}`.

Не проверено: живой источник с реальными данными (профиль/рейтинг ВАВТ или ВШЭ) — playbook
для них пишет агент (PR 6); UI панелей; перечень иконок нужно сверить с реестром после PR 4.
