/**
 * Синтетические блоки агента и снимки заданий для витрины. Все блоки проходят проверку формы `parseBlock`;
 * ничего из этого не приходит с настоящего сервера.
 */

import type { Login } from "@/features/agent-blocks/types";
import { type ParsedBlock, parseBlock } from "@/features/jobs/blocks";
import { JobManager } from "@/features/jobs/job-manager";
import { applyEvent, initialJob, type JobSnapshot } from "@/features/jobs/job-state";
import { NOW } from "./fixtures";

const ID = (n: number) => `01JSTYKE${String(n).padStart(18, "0")}`;
const AT = new Date(NOW - 60_000).toISOString();

function block(raw: object): ParsedBlock {
  const parsed = parseBlock(raw);
  if (!parsed) throw new Error("не блок");
  return parsed;
}

export const SHOWCASE_LOGIN: Login = {
  type: "login",
  id: ID(13),
  at: AT,
  connectionId: ID(12),
  ticket: "demo-ticket",
  wsUrl: "/api/v1/login-sessions/DEMO/ws",
  expiresAt: new Date(NOW + 600_000).toISOString(),
  hint: "Вуз показал проверку «я не робот»: вход здесь делаете вы.",
};

export const SHOWCASE_BLOCKS = {
  text: block({
    type: "text",
    id: ID(1),
    at: AT,
    markdown:
      "Следующая пара — **Алгоритмы** в 11:10, ауд. 214. Домашнее задание 3 нужно сдать сегодня до 19:05.",
    sources: [{ title: "Расписание — ЛК вуза", url: "https://lk.fakeuni.test/timetable", fetchedAt: AT }],
  }),
  choiceSingle: block({
    type: "choice",
    id: ID(2),
    at: AT,
    question: "Какой это вуз?",
    mode: "single",
    required: true,
    allowOther: true,
    options: [
      { value: "hse", label: "Высшая школа экономики", hint: "Основная программа" },
      { value: "cu", label: "Центральный университет" },
    ],
  }),
  choiceMulti: block({
    type: "choice",
    id: ID(3),
    at: AT,
    question: "Это ваши предметы в этом семестре?",
    mode: "multi",
    required: false,
    options: [
      { value: "algo", label: "Алгоритмы и структуры данных", hint: "Лекции, вторник и четверг" },
      { value: "db", label: "Базы данных" },
      { value: "eng", label: "Английский язык" },
    ],
  }),
  inputText: block({
    type: "input",
    id: ID(4),
    at: AT,
    question: "Вход в lk.fakeuni.test. Логин",
    inputKind: "text",
    required: true,
    secret: false,
    autocomplete: "username",
    placeholder: "student@fakeuni.test",
  }),
  inputUrl: block({
    type: "input",
    id: ID(5),
    at: AT,
    question: "Где у вас расписание? Вставьте ссылку на страницу.",
    inputKind: "url",
    required: false,
    secret: false,
    autocomplete: "url",
  }),
  inputSecret: block({
    type: "input",
    id: ID(6),
    at: AT,
    question: "Вход в lk.fakeuni.test. Пароль",
    inputKind: "password",
    required: true,
    secret: true,
    autocomplete: "current-password",
    maxLength: 200,
  }),
  inputCode: block({
    type: "input",
    id: ID(7),
    at: AT,
    question: "Код из SMS",
    inputKind: "code",
    required: true,
    secret: true,
    autocomplete: "one-time-code",
    maxLength: 6,
  }),
  confirm: block({
    type: "confirm",
    id: ID(8),
    at: AT,
    question: "Подключить ещё и календарь из личного кабинета?",
    confirmLabel: "Подключить",
    cancelLabel: "Не нужно",
  }),
  summary: block({
    type: "summary",
    id: ID(9),
    at: AT,
    title: "Что подключено",
    groups: [
      {
        title: "Подключено",
        items: [
          { label: "Личный кабинет", value: "lk.fakeuni.test", confidence: "confirmed" },
          {
            label: "Расписание",
            value: "42 занятия до конца семестра",
            confidence: "confirmed",
            sourceUrl: "https://lk.fakeuni.test/timetable",
          },
          { label: "Сроки заданий", value: "7 заданий, у 2 срок не указан", confidence: "guess" },
        ],
      },
      { title: "Не подключено", items: [{ label: "Оценки", value: "Раздел требует отдельного входа" }] },
    ],
  }),
  approval: block({
    type: "approval",
    id: ID(10),
    at: AT,
    proposal: {
      id: ID(11),
      connectionId: ID(12),
      intent: "Записаться на консультацию по базам данных",
      reversible: true,
      expiresAt: new Date(NOW + 600_000).toISOString(),
      request: {
        method: "POST",
        url: "https://lk.fakeuni.test/consultations",
        bodyKind: "form",
        fields: [
          { name: "course", label: "Предмет", value: "Базы данных", editable: false },
          { name: "comment", label: "Комментарий", value: "", editable: true },
        ],
      },
    },
  }),
  login: block(SHOWCASE_LOGIN),
  wait: block({
    type: "wait",
    id: ID(14),
    at: AT,
    message: "Подтвердите вход в приложении на телефоне",
    expiresAt: new Date(NOW + 150_000).toISOString(),
  }),
  handoff: block({
    type: "handoff",
    id: ID(15),
    at: AT,
    title: "Заявка на справку",
    url: "https://fakeuni.test/requests/new",
    text: "Прошу выдать справку об обучении для предоставления по месту требования.",
  }),
  unknown: block({ type: "carousel", id: ID(16), at: AT, markdown: "Три карточки с расписанием на неделю." }),
} as const;

const ev = (event: string, data: unknown, id: string) => ({ event, data: JSON.stringify(data), id });

function snapshot(
  jobId: string,
  events: ReturnType<typeof ev>[],
  patch: Partial<JobSnapshot> = {},
): JobSnapshot {
  const job = events.reduce((state, event) => applyEvent(state, event), initialJob(jobId));
  return { ...job, link: "live", ...patch };
}

const startedAt = new Date(NOW - 72_000).toISOString();
const status = (state: string, stage: string, n: number) =>
  ev("status", { state, stage, startedAt }, String(n));
const step = (n: number, stage: string, state: string, detail?: string) =>
  ev(
    "block",
    {
      type: "thinking",
      id: ID(100 + n),
      at: new Date(NOW - 70_000 + n * 6_000).toISOString(),
      stage,
      state,
      detail,
    },
    `s${n}`,
  );

export const SHOWCASE_JOBS = {
  question: snapshot("01JSTYLEJOB000000000000A1", [
    status("waiting_user", "Жду ваш логин", 1),
    step(1, "Ищу личный кабинет", "ok"),
    step(2, "Открываю страницу входа lk.fakeuni.test", "ok"),
    ev("block", SHOWCASE_BLOCKS.inputText, "b1"),
  ]),
  phone: snapshot("01JSTYLEJOB000000000000A2", [
    status("waiting_user", "Жду подтверждения входа на телефоне", 1),
    step(1, "Ищу личный кабинет", "ok"),
    step(2, "Вхожу в личный кабинет", "running"),
    ev("block", SHOWCASE_BLOCKS.wait, "b1"),
  ]),
  done: snapshot("01JSTYLEJOB000000000000A3", [
    status("running", "Готово", 1),
    step(1, "Ищу личный кабинет", "ok"),
    step(2, "Читаю список курсов", "ok"),
    step(3, "Читаю расписание", "ok", "Нашёл занятия на семестр"),
    ev("block", SHOWCASE_BLOCKS.summary, "b1"),
    ev("done", { result: {} }, "b2"),
  ]),
  failed: snapshot("01JSTYLEJOB000000000000A4", [
    status("running", "Открываю расписание", 1),
    step(1, "Ищу личный кабинет", "ok"),
    step(2, "Открываю расписание", "failed", "Страница не ответила"),
    ev("error", { code: "egress_blocked", detail: "Хост вне списка", host: "cdn.partner-lms.test" }, "e1"),
  ]),
  reconnecting: snapshot(
    "01JSTYLEJOB000000000000A5",
    [
      status("running", "Читаю список курсов", 1),
      step(1, "Ищу личный кабинет", "ok"),
      step(2, "Читаю список курсов", "running"),
    ],
    { link: "reconnecting" },
  ),
} as const;

/** Менеджер для витрины: ответы только помечаются отвеченными, сети нет. */
export class DemoJobManager extends JobManager {
  override async answer(
    jobId: string,
    blockId: string,
    _values: unknown,
    shown: string | null,
  ): Promise<void> {
    this.patchAnswer(jobId, blockId, { status: "answered", shown, error: null });
  }
  override async approve(jobId: string, blockId: string): Promise<void> {
    this.patchAnswer(jobId, blockId, { status: "answered", shown: null, error: null, outcome: "approved" });
  }
  override async reject(jobId: string, blockId: string): Promise<void> {
    this.patchAnswer(jobId, blockId, { status: "answered", shown: null, error: null, outcome: "rejected" });
  }
  override async cancel(): Promise<void> {}
}
