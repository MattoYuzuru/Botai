import { delay, HttpResponse, http } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { api, call } from "@/api/client";
import { JobSim } from "@/mocks/job-sim";
import { createTestServer } from "@/test/server";
import { JobManager } from "./job-manager";
import { pendingQuestions } from "./job-state";

const { db, server } = createTestServer("connected");

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

const fastManager = () =>
  new JobManager(
    () => window.location.origin,
    () => 5,
  );

async function startJob(variant: typeof db.jobVariant = "basic") {
  db.jobVariant = variant;
  const space = db.spaces[0];
  if (!space) throw new Error("нет пространства");
  const started = await call(
    api.POST("/api/v1/spaces/{space_id}/connections", {
      params: { path: { space_id: space.id } },
      body: { title: "Личный кабинет", kind: "portal", url: null },
    }),
  );
  return started;
}

async function until<T>(read: () => T | undefined | false, label: string): Promise<T> {
  const deadline = Date.now() + 4000;
  while (Date.now() < deadline) {
    const value = read();
    if (value) return value;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error(`не дождались: ${label}`);
}

describe("поток задания подключения на MSW", () => {
  it.each([
    [204, "before"],
    [409, "before"],
    [409, "after"],
  ] as const)(
    "native answer HTTP%s и SSE %s: ручной handoff не выдумывает «Введено»",
    async (status, order) => {
      const jobId = "01JAAAAAAAAAAAAAAAAAAAAA91";
      const connectionId = "01JAAAAAAAAAAAAAAAAAAAAA92";
      const blockId = "01JAAAAAAAAAAAAAAAAAAAAA93";
      const sim = new JobSim(jobId, () => Date.now(), 0);
      db.sims.set(jobId, sim);
      sim.status("Жду пароль", "waiting_user");
      sim.block({
        type: "input",
        id: blockId,
        at: new Date().toISOString(),
        question: "Пароль",
        inputKind: "password",
        secret: true,
        autocomplete: "current-password",
        required: true,
      });
      let release: (() => void) | undefined;
      const pending = new Promise<void>((resolve) => {
        release = resolve;
      });
      server.use(
        http.post(`*/api/v1/jobs/${jobId}/answers`, async () => {
          await pending;
          return status === 204
            ? new HttpResponse(null, { status })
            : HttpResponse.json({ code: "block_answered", detail: "Этот шаг закрыт" }, { status });
        }),
      );
      const manager = fastManager();
      manager.start(jobId);
      await until(() => manager.get(jobId)?.blocks.length === 1, "native вопрос");
      const answer = manager.answer(jobId, blockId, "handoff-only-secret", null, connectionId);
      const handoff = async () => {
        sim.block({
          type: "login",
          id: "01JAAAAAAAAAAAAAAAAAAAAA94",
          at: new Date().toISOString(),
          connectionId,
          ticket: "fixture-one-time-ticket",
          wsUrl: "/api/v1/login-sessions/L/ws",
          expiresAt: new Date(Date.now() + 60_000).toISOString(),
        });
        await until(() => manager.get(jobId)?.blocks.some((block) => block.type === "login"), "SSE handoff");
      };
      try {
        if (order === "before") await handoff();
        release?.();
        await answer;
        if (order === "after") await handoff();
        expect(manager.get(jobId)?.answers[blockId]).toBeUndefined();
        expect(JSON.stringify(manager.get(jobId))).not.toContain("handoff-only-secret");
        expect(pendingQuestions(manager.get(jobId) ?? ({} as never), connectionId)).toEqual([]);
      } finally {
        release?.();
        manager.stop(jobId);
      }
    },
  );

  it("весь путь: логин, секретный пароль, выбор предметов, итог", async () => {
    const manager = fastManager();
    const { jobId } = await startJob("basic");
    manager.start(jobId);

    const login = await until(
      () => pendingQuestions(manager.get(jobId) ?? ({} as never))[0],
      "вопрос про логин",
    );
    expect(login).toMatchObject({
      type: "input",
      inputKind: "text",
      secret: false,
      autocomplete: "username",
    });
    await manager.answer(jobId, login.id, "anna", "anna");
    expect(manager.get(jobId)?.answers[login.id]).toMatchObject({ status: "answered", shown: "anna" });

    const password = await until(
      () => pendingQuestions(manager.get(jobId) ?? ({} as never)).find((block) => block.id !== login.id),
      "вопрос про пароль",
    );
    expect(password).toMatchObject({
      type: "input",
      secret: true,
      inputKind: "password",
      autocomplete: "current-password",
    });
    await manager.answer(jobId, password.id, "correct-horse", null);

    const choice = await until(
      () => pendingQuestions(manager.get(jobId) ?? ({} as never)).find((block) => block.type === "choice"),
      "вопрос про предметы",
    );
    await manager.answer(jobId, choice.id, ["algo", "db"], "Алгоритмы, Базы данных");

    // Одно сообщение-подтверждение: сводка, ближайшие пары и вопрос.
    const confirm = await until(
      () => pendingQuestions(manager.get(jobId) ?? ({} as never)).find((block) => block.type === "confirm"),
      "подтверждение",
    );
    expect(confirm).toMatchObject({ question: expect.stringMatching(/^Всё верно\?/) });
    expect(confirm.type === "confirm" && confirm.details).toContain("Ближайшие пары:");
    await manager.answer(jobId, confirm.id, true, "Да, всё верно");

    const done = await until(() => {
      const job = manager.get(jobId);
      return job?.result ? job : undefined;
    }, "завершение");
    expect(
      done.blocks.some((block) => block.type === "text" && block.markdown.startsWith("Готово! Расписание")),
    ).toBe(true);
    expect(done.link).toBe("closed");
    expect(done.result).toMatchObject({ connectionId: expect.any(String) });

    // Секрет не попал ни в состояние задания, ни в журнал событий сервера.
    expect(JSON.stringify(done)).not.toContain("correct-horse");
    const sim = db.sims.get(jobId);
    expect(JSON.stringify(sim?.events)).not.toContain("correct-horse");
    expect(sim?.sealed.get(password.id)).toBe("correct-horse");
    manager.stop(jobId);
  });

  it("повторный ответ на отвеченный блок (409 block_answered) считается данным, а не ошибкой", async () => {
    const manager = fastManager();
    const { jobId } = await startJob();
    manager.start(jobId);
    const login = await until(() => pendingQuestions(manager.get(jobId) ?? ({} as never))[0], "вопрос");
    await manager.answer(jobId, login.id, "anna", "anna");
    await manager.answer(jobId, login.id, "anna", "anna");
    expect(manager.get(jobId)?.answers[login.id]?.status).toBe("answered");
    manager.stop(jobId);
  });

  it("ответ на несуществующий блок — ошибка человеческим текстом, значение не сохраняется", async () => {
    const manager = fastManager();
    const { jobId } = await startJob();
    manager.start(jobId);
    await until(() => manager.get(jobId)?.blocks.length, "первые блоки");
    await manager.answer(jobId, "01JAAAAAAAAAAAAAAAAAAAAAAA", "секрет", null);
    const answer = manager.get(jobId)?.answers["01JAAAAAAAAAAAAAAAAAAAAAAA"];
    expect(answer?.status).toBe("failed");
    expect(answer?.error).toBe("Блок не найден");
    expect(JSON.stringify(manager.get(jobId))).not.toContain("секрет");
    manager.stop(jobId);
  });

  it("сбой задания: код и текст из problem+json, поток закрыт", async () => {
    const manager = fastManager();
    const { jobId } = await startJob("failure");
    manager.start(jobId);
    const login = await until(() => pendingQuestions(manager.get(jobId) ?? ({} as never))[0], "логин");
    await manager.answer(jobId, login.id, "anna", "anna");
    const pwd = await until(
      () => pendingQuestions(manager.get(jobId) ?? ({} as never)).find((b) => b.id !== login.id),
      "пароль",
    );
    await manager.answer(jobId, pwd.id, "x", null);
    const failed = await until(() => manager.get(jobId)?.state === "failed" && manager.get(jobId), "сбой");
    expect(failed.problem).toMatchObject({
      code: "source_unavailable",
      detail: "Сайт вуза не ответил после нескольких попыток.",
    });
    expect(failed.link).toBe("closed");
    manager.stop(jobId);
  });

  it("отмена задания закрывает поток состоянием cancelled", async () => {
    const manager = fastManager();
    const { jobId } = await startJob();
    manager.start(jobId);
    await until(() => pendingQuestions(manager.get(jobId) ?? ({} as never))[0], "вопрос");
    await manager.cancel(jobId);
    const cancelled = await until(
      () => manager.get(jobId)?.state === "cancelled" && manager.get(jobId),
      "отмена",
    );
    expect(cancelled.link).toBe("closed");
    manager.stop(jobId);
  });

  it("неизвестное задание: поток не крутится вечно, человеку — текст", async () => {
    const manager = fastManager();
    manager.start("01JAAAAAAAAAAAAAAAAAAAAAAA");
    const failed = await until(
      () =>
        manager.get("01JAAAAAAAAAAAAAAAAAAAAAAA")?.state === "failed" &&
        manager.get("01JAAAAAAAAAAAAAAAAAAAAAAA"),
      "404",
    );
    expect(failed.problem?.code).toBe("not_found");
    expect(failed.link).toBe("closed");
    manager.stop("01JAAAAAAAAAAAAAAAAAAAAAAA");
  });

  it("сервер ещё не умеет поток (501): задание помечено сбоем без повторов", async () => {
    server.use(
      http.get("*/api/v1/jobs/:id/events", () =>
        HttpResponse.json(
          { code: "not_implemented", detail: "job_events: ещё не реализовано" },
          { status: 501 },
        ),
      ),
    );
    const manager = fastManager();
    manager.start("01JAAAAAAAAAAAAAAAAAAAAAAB");
    const failed = await until(
      () =>
        manager.get("01JAAAAAAAAAAAAAAAAAAAAAAB")?.state === "failed" &&
        manager.get("01JAAAAAAAAAAAAAAAAAAAAAAB"),
      "501",
    );
    expect(failed.problem?.code).toBe("not_implemented");
    manager.stop("01JAAAAAAAAAAAAAAAAAAAAAAB");
  });
});

describe("переподключение", () => {
  it("обрыв потока: клиент возвращается с Last-Event-ID и не дублирует блоки", async () => {
    const seen: (string | null)[] = [];
    const sim = new JobSim("01JAAAAAAAAAAAAAAAAAAAAAAC", () => Date.now(), 0);
    db.sims.set(sim.jobId, sim);
    sim.status("Ищу кабинет");
    sim.block({
      type: "thinking",
      id: "01JAAAAAAAAAAAAAAAAAAAAAA1",
      at: new Date().toISOString(),
      stage: "Ищу кабинет",
      state: "running",
    });

    let connections = 0;
    server.use(
      http.get("*/api/v1/jobs/:id/events", ({ request }) => {
        connections += 1;
        seen.push(request.headers.get("last-event-id"));
        if (connections === 1) {
          // Первое соединение: отдаём накопленное и обрываем без завершающего события.
          const text = sim.events
            .map((e) => `id: ${e.id}\nevent: ${e.event}\ndata: ${JSON.stringify(e.data)}\n\n`)
            .join("");
          return new HttpResponse(text, { headers: { "content-type": "text/event-stream" } });
        }
        return new HttpResponse(sim.stream(Number(request.headers.get("last-event-id") ?? 0)), {
          headers: { "content-type": "text/event-stream" },
        });
      }),
    );

    const manager = fastManager();
    manager.start(sim.jobId);
    await until(() => manager.get(sim.jobId)?.blocks.length === 1, "первое соединение");
    await delay(30);
    sim.block({
      type: "thinking",
      id: "01JAAAAAAAAAAAAAAAAAAAAAA2",
      at: new Date().toISOString(),
      stage: "Читаю расписание",
      state: "running",
    });
    sim.status("Готово", "done");
    sim.emit("done", { result: {} });

    const job = await until(
      () => manager.get(sim.jobId)?.state === "done" && manager.get(sim.jobId),
      "после переподключения",
    );
    expect(job.blocks.map((block) => block.id)).toEqual([
      "01JAAAAAAAAAAAAAAAAAAAAAA1",
      "01JAAAAAAAAAAAAAAAAAAAAAA2",
    ]);
    expect(connections).toBeGreaterThanOrEqual(2);
    expect(seen[0]).toBeNull();
    expect(Number(seen[1])).toBeGreaterThan(0);
    manager.stop(sim.jobId);
  });
});
