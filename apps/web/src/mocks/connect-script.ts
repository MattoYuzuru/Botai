import type { AgentUiBlock } from "@/api/agent-ui.gen";
import type { MockDb } from "./db";
import { ulid } from "./db";
import { JobCancelledError, type JobSim } from "./job-sim";

/**
 * Сценарии подключения вуза для мок-режима. Повторяют поведение настоящего LoginFlow: логин и пароль
 * приходят блоками `input` (пароль — `secret`), вход по push — блоком `wait`, итог — `summary`.
 */
export type JobVariant = "basic" | "wrong-password" | "push" | "failure" | "approval";

export const JOB_VARIANTS: readonly JobVariant[] = ["basic", "wrong-password", "push", "failure", "approval"];

const HOST = "lk.fakeuni.test";

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

    const passwordBlock = (question: string): AgentUiBlock => ({
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
      type: "summary",
      id: ulid(),
      at: now,
      title: "Что подключено",
      groups: [
        {
          title: "Подключено",
          items: [
            { label: "Личный кабинет", value: HOST, confidence: "confirmed" },
            {
              label: "Расписание",
              value: "42 занятия до конца семестра",
              confidence: "confirmed",
              sourceUrl: `https://${HOST}/timetable`,
            },
            { label: "Сроки заданий", value: "7 заданий, у 2 срок не указан", confidence: "guess" },
          ],
        },
        { title: "Не подключено", items: [{ label: "Оценки", value: "Раздел требует отдельного входа" }] },
      ],
    });
    sim.status("Готово");
    sim.emit("done", { result: { connectionId } });
  } catch (error) {
    if (!(error instanceof JobCancelledError)) throw error;
  }
}
