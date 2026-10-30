import type { KnownBlock } from "@/features/jobs/blocks";
import type { MockDb } from "./db";
import { ulid } from "./db";
import { JobCancelledError, type JobSim } from "./job-sim";

/**
 * Сценарии подключения вуза для мок-режима. Повторяют поведение настоящего LoginFlow: логин и пароль
 * приходят блоками `input` (пароль — `secret`), вход по push — блоком `wait`, затем одно сообщение-подтверждение
 * (`confirm` со сводкой и ближайшими парами) и дружелюбный итог.
 */
export type JobVariant = "basic" | "wrong-password" | "push" | "failure" | "approval";

export const JOB_VARIANTS: readonly JobVariant[] = ["basic", "wrong-password", "push", "failure", "approval"];

const HOST = "lk.fakeuni.test";

const dayTitle = (ms: number) => {
  const text = new Intl.DateTimeFormat("ru-RU", {
    timeZone: "Europe/Moscow",
    weekday: "long",
    day: "numeric",
    month: "long",
  }).format(ms);
  return text.charAt(0).toUpperCase() + text.slice(1);
};

/** Текст подтверждения, как у сервера: что найдено словами и пары двух ближайших дней. */
function confirmDetails(now: number): string {
  const day = 86_400_000;
  return [
    "Нашла в «Личный кабинет»:",
    "",
    "- **Расписание:** 42 занятия с 26 октября по 20 декабря — 8 недель",
    "- **Дедлайны:** 7 заданий со сроками",
    "",
    "Ближайшие пары:",
    "",
    `**${dayTitle(now + day)}**`,
    "- 09:30–10:50 · Алгоритмы и структуры данных · А-101",
    "- 11:10–12:30 · Базы данных · В-310",
    "",
    `**${dayTitle(now + 3 * day)}**`,
    "- 15:10–16:30 · Английский язык · online",
  ].join("\n");
}

export async function runConnect(
  sim: JobSim,
  db: MockDb,
  connectionId: string,
  variant: JobVariant,
): Promise<void> {
  const at = () => new Date(db.now()).toISOString();
  const step = async (stage: string, ms = 700, detail?: string) => {
    const id = ulid();
    sim.status(stage);
    sim.block({ type: "thinking", id, at: at(), stage, state: "running", detail });
    await sim.pause(ms);
    sim.update(id, { state: "ok" });
    return id;
  };

  try {
    sim.status("Ищу личный кабинет");
    await step("Ищу личный кабинет");
    await step(`Открываю страницу входа ${HOST}`);

    await sim.ask(
      {
        type: "input",
        id: ulid(),
        at: at(),
        question: `Вход в ${HOST}. Логин`,
        inputKind: "text",
        required: true,
        secret: false,
        autocomplete: "username",
        maxLength: 200,
      },
      "Жду логин",
    );

    const passwordBlock = (question: string): KnownBlock => ({
      type: "input",
      id: ulid(),
      at: at(),
      question,
      inputKind: "password",
      required: true,
      secret: true,
      autocomplete: "current-password",
      maxLength: 200,
    });
    await sim.ask(passwordBlock(`Вход в ${HOST}. Пароль`), "Жду пароль");

    if (variant === "wrong-password") {
      const failed = ulid();
      sim.block({
        type: "thinking",
        id: failed,
        at: at(),
        stage: "Вуз не принял пароль",
        state: "failed",
        detail: "Проверьте раскладку и язык ввода",
      });
      await sim.pause(500);
      await sim.ask(
        passwordBlock(`Вуз не принял данные. Проверьте и введите ещё раз: Пароль (попытка 2 из 3)`),
        "Жду пароль ещё раз",
      );
    }

    if (variant === "failure") {
      const connection = db.connections.find((item) => item.id === connectionId);
      if (connection) connection.status = "error";
      sim.emit("error", {
        type: "https://botai.mnema.app/errors/source_unavailable",
        title: "source_unavailable",
        status: 502,
        code: "source_unavailable",
        detail: "Сайт вуза не ответил после нескольких попыток.",
      });
      return;
    }

    if (variant === "push") {
      sim.block({
        type: "wait",
        id: ulid(),
        at: at(),
        message: "Подтвердите вход в приложении на телефоне: сайт вуза ждёт подтверждения.",
        expiresAt: new Date(db.now() + 120_000).toISOString(),
      });
      sim.status("Жду подтверждения входа на телефоне", "waiting_user");
      await sim.pause(2200);
      sim.status("Подтверждение получено, продолжаю вход");
    }

    await step("Читаю список курсов", 800);
    await step("Читаю расписание", 800, "Нашёл занятия на семестр");

    await sim.ask(
      {
        type: "choice",
        id: ulid(),
        at: at(),
        question: "Это ваши предметы в этом семестре?",
        mode: "multi",
        required: true,
        allowOther: false,
        options: [
          { value: "algo", label: "Алгоритмы и структуры данных", hint: "Лекции, вторник и четверг" },
          { value: "db", label: "Базы данных" },
          { value: "eng", label: "Английский язык" },
        ],
      },
      "Жду ответа про предметы",
    );

    if (variant === "approval") {
      const proposal = ulid();
      await sim.ask(
        {
          type: "approval",
          id: ulid(),
          at: at(),
          proposal: {
            id: proposal,
            connectionId,
            intent: "Записаться на консультацию по базам данных",
            reversible: true,
            expiresAt: new Date(db.now() + 600_000).toISOString(),
            request: {
              method: "POST",
              url: `https://${HOST}/consultations`,
              bodyKind: "form",
              fields: [
                { name: "course", label: "Предмет", value: "Базы данных", editable: false },
                { name: "comment", label: "Комментарий", value: "", editable: true },
              ],
            },
          },
        },
        "Жду вашего подтверждения",
      );
    }

    const confirmed = await sim.ask(
      {
        type: "confirm",
        id: ulid(),
        at: at(),
        question: "Всё верно? После подтверждения данные будут обновляться автоматически.",
        confirmLabel: "Да, всё верно",
        cancelLabel: "Нет, не то",
        details: confirmDetails(db.now()),
      },
      "Жду подтверждения",
    );
    if (confirmed !== true) {
      sim.block({ type: "text", id: ulid(), at: at(), markdown: "Хорошо, ничего не сохраняю." });
      sim.status("Готово");
      sim.emit("done", { result: { connectionId, status: "error" } });
      return;
    }

    await step("Сохраняю найденное", 500);

    const now = at();
    const connection = db.connections.find((item) => item.id === connectionId);
    if (connection) {
      connection.status = "active";
      connection.hasPassword = true;
      connection.lastSyncAt = now;
      connection.adapter = "recipe";
    }

    sim.block({
      type: "text",
      id: ulid(),
      at: now,
      markdown:
        "Готово! Расписание Фейкуни подключено: 42 занятия на 17 недель вперёд. " +
        "Обновляю само каждые 3 часа — спросите, например, «что у меня завтра?»",
    });
    sim.status("Готово");
    sim.emit("done", { result: { connectionId } });
  } catch (error) {
    if (!(error instanceof JobCancelledError)) throw error;
  }
}
