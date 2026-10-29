import { describe, expect, it } from "vitest";
import { ApiError, humanMessage } from "@/api/errors";
import {
  activeAttention,
  applyEvent,
  initialJob,
  isTerminal,
  jobFailed,
  jobProblem,
  needsHuman,
  pendingQuestions,
  supersededAuthQuestions,
  thinkingSteps,
} from "./job-state";

const ID = (n: number) => `01JAAAAAAAAAAAAAAAAAAAAA${String(n).padStart(2, "0")}`;
const AT = "2026-10-27T10:00:00Z";

const ev = (event: string, data: unknown, id?: string) => ({ event, data: JSON.stringify(data), id });
const thinking = (n: number, stage: string, state?: string) => ({
  type: "thinking",
  id: ID(n),
  at: AT,
  stage,
  state,
});
const input = (n: number, extra: object = {}) => ({
  type: "input",
  id: ID(n),
  at: AT,
  question: "Логин",
  inputKind: "text",
  required: true,
  ...extra,
});

describe("журнал события → состояние", () => {
  it("status: состояние, стадия и время старта; Last-Event-ID запоминается", () => {
    const job = applyEvent(
      initialJob("J"),
      ev("status", { state: "running", stage: "Читаю список курсов", startedAt: AT }, "5"),
    );
    expect(job).toMatchObject({
      state: "running",
      stage: "Читаю список курсов",
      startedAt: Date.parse(AT),
      lastEventId: "5",
    });
  });

  it("block идемпотентен по id: повторная доставка заменяет, а не дублирует", () => {
    let job = initialJob("J");
    job = applyEvent(job, ev("block", thinking(1, "Ищу кабинет"), "1"));
    job = applyEvent(job, ev("block", thinking(2, "Открываю вход"), "2"));
    job = applyEvent(job, ev("block", thinking(1, "Ищу кабинет", "ok"), "3"));
    expect(job.blocks.map((block) => block.id)).toEqual([ID(1), ID(2)]);
    expect(job.blocks[0]).toMatchObject({ state: "ok" });
  });

  it("block_update сливает патч в блок по id", () => {
    let job = applyEvent(initialJob("J"), ev("block", thinking(1, "Ищу кабинет")));
    job = applyEvent(job, ev("block_update", { id: ID(1), patch: { state: "ok", detail: "Нашёл" } }));
    expect(job.blocks[0]).toMatchObject({ state: "ok", detail: "Нашёл" });
    // Обновление несуществующего блока ничего не ломает.
    expect(applyEvent(job, ev("block_update", { id: ID(9), patch: { state: "ok" } })).blocks).toHaveLength(1);
  });

  it("error: задание упало, код и человеческий текст сохранены, технические поля отброшены", () => {
    const job = applyEvent(
      initialJob("J"),
      ev("error", {
        type: "x",
        title: "egress_blocked",
        status: 403,
        code: "egress_blocked",
        detail: "Хост вне списка",
        host: "evil.test",
        traceId: "t",
      }),
    );
    expect(job.state).toBe("failed");
    expect(job.problem).toEqual({
      code: "egress_blocked",
      detail: "Хост вне списка",
      extra: { host: "evil.test" },
    });
  });

  it("done: итог задания", () => {
    const job = applyEvent(initialJob("J"), ev("done", { result: { connectionId: "c1" } }));
    expect(job).toMatchObject({ state: "done", result: { connectionId: "c1" } });
    expect(isTerminal(job.state)).toBe(true);
  });

  it("мусор в событиях не ломает состояние", () => {
    const job = initialJob("J");
    expect(applyEvent(job, { event: "status", data: "не json" })).toEqual(job);
    expect(applyEvent(job, { event: "block", data: "[]" })).toEqual(job);
    expect(applyEvent(job, { event: "неизвестное", data: "{}", id: "9" })).toEqual({
      ...job,
      lastEventId: "9",
    });
    expect(applyEvent(job, ev("status", { state: "взрыв" })).state).toBe("queued");
  });

  it("неизвестный тип блока попадает в журнал как UnknownBlock", () => {
    const job = applyEvent(
      initialJob("J"),
      ev("block", { type: "carousel", id: ID(1), at: AT, text: "что-то" }),
    );
    expect(job.blocks[0]).toMatchObject({ type: "unknown", rawType: "carousel" });
  });
});

describe("вопросы и ожидание человека", () => {
  it("реальный matching LoginBlock заменяет только старый auth input без выдуманного ответа", () => {
    let job = applyEvent(initialJob("J"), ev("block", input(1, { autocomplete: "username" })));
    job = applyEvent(job, ev("block", input(2, { question: "Поиск курсов" })));
    job = applyEvent(job, ev("block", input(3, { inputKind: "url" })));
    job = applyEvent(
      job,
      ev("block", {
        type: "login",
        id: ID(4),
        at: AT,
        connectionId: ID(9),
        ticket: "one-time",
        wsUrl: "/api/v1/login-sessions/L/ws",
        expiresAt: AT,
      }),
    );
    expect(pendingQuestions(job, ID(8)).map((block) => block.id)).toEqual([ID(1), ID(2), ID(3)]);
    expect(pendingQuestions(job).map((block) => block.id)).toEqual([ID(1), ID(2), ID(3)]);
    expect(pendingQuestions(job, ID(9)).map((block) => block.id)).toEqual([ID(2), ID(3)]);
    expect([...supersededAuthQuestions(job, ID(9))]).toEqual([ID(1)]);
    expect(job.answers).toEqual({});
    job = applyEvent(job, ev("block", input(5, { inputKind: "password" })));
    expect(pendingQuestions(job, ID(9)).map((block) => block.id)).toEqual([ID(2), ID(3), ID(5)]);
  });

  it("done с legacy login_failed result остаётся ошибкой, а обычный done — успехом", () => {
    const failed = applyEvent(
      initialJob("J"),
      ev("done", { result: { status: "error", detail: "login_failed" } }),
    );
    expect(jobFailed(failed)).toBe(true);
    expect(jobProblem(failed)).toMatchObject({ code: "login_failed" });
    const done = applyEvent(initialJob("J"), ev("done", { result: { status: "ok" } }));
    expect(jobFailed(done)).toBe(false);
    expect(jobProblem(done)).toBeNull();
  });

  it.each([
    ["source_auth_required", "Нужно снова войти в личный кабинет вуза."],
    [
      "recipe_rejected",
      "Описание подключения не прошло проверку или не было подтверждено. Подключение не завершено.",
    ],
  ])("done с %s сохраняет причину для сообщения в истории", (code, message) => {
    const job = applyEvent(initialJob("J"), ev("done", { result: { status: "error", detail: code } }));
    const problem = jobProblem(job);
    expect(jobFailed(job)).toBe(true);
    expect(problem).toEqual({ code, detail: "", extra: {} });
    expect(humanMessage(new ApiError(500, problem?.code ?? "unknown", problem?.detail ?? ""))).toBe(message);
  });

  it.each([
    "source_unavailable",
    "source_rate_limited",
    "source_bot_protection",
    "recipe_drift",
    "egress_blocked",
    "quota_exceeded",
    "injection_blocked",
    "llm_unavailable",
    "not_found",
    "validation",
  ])("done сохраняет известный код подключения %s", (code) => {
    const job = applyEvent(initialJob("J"), ev("done", { result: { status: "error", detail: code } }));
    expect(jobProblem(job)).toEqual({ code, detail: "", extra: {} });
  });

  it.each(["private diagnostic: student@example.test", "source_new_error", null, { private: "value" }])(
    "произвольный detail %j не превращается в код или сообщение пользователю",
    (detail) => {
      const job = applyEvent(initialJob("J"), ev("done", { result: { status: "error", detail } }));
      const problem = jobProblem(job);
      expect(problem).toEqual({ code: "unknown", detail: "", extra: {} });
      expect(humanMessage(new ApiError(500, problem?.code ?? "unknown", problem?.detail ?? ""))).toBe(
        "У BotAI сейчас неполадка. Мы уже разбираемся.",
      );
    },
  );

  it("вопрос без ответа ждёт, отвеченный — нет; после завершения вопросов не остаётся", () => {
    let job = applyEvent(
      initialJob("J"),
      ev("status", { state: "waiting_user", stage: "Жду логин", startedAt: AT }),
    );
    job = applyEvent(job, ev("block", input(1)));
    expect(pendingQuestions(job).map((block) => block.id)).toEqual([ID(1)]);
    expect(needsHuman(job)).toBe(true);
    job = { ...job, answers: { [ID(1)]: { status: "answered", shown: "anna", error: null } } };
    expect(pendingQuestions(job)).toEqual([]);
    job = applyEvent(job, ev("block", input(2)));
    job = applyEvent(job, ev("done", { result: {} }));
    expect(pendingQuestions(job)).toEqual([]);
    expect(needsHuman(job)).toBe(false);
  });

  it("wait и login требуют человека, пока задание идёт", () => {
    let job = applyEvent(initialJob("J"), ev("status", { state: "running", stage: "Вхожу", startedAt: AT }));
    job = applyEvent(job, ev("block", { type: "wait", id: ID(1), at: AT, message: "Подтвердите вход" }));
    expect(needsHuman(job)).toBe(true);
  });

  it("wait и login закрываются, когда задание вернулось к работе и дописало шаги, или пришёл вопрос", () => {
    let job = applyEvent(initialJob("J"), ev("status", { state: "running", stage: "Вхожу", startedAt: AT }));
    job = applyEvent(job, ev("block", { type: "wait", id: ID(1), at: AT, message: "Подтвердите вход" }));
    job = applyEvent(job, ev("status", { state: "waiting_user", stage: "Жду подтверждения", startedAt: AT }));
    expect(activeAttention(job)).toHaveLength(1);
    // Пока ждём, шаги в журнале не закрывают ожидание.
    job = applyEvent(job, ev("block", thinking(2, "Жду подтверждения", "running")));
    expect(activeAttention(job)).toHaveLength(1);
    // Работа продолжилась: подтверждение прошло.
    job = applyEvent(job, ev("status", { state: "running", stage: "Читаю курсы", startedAt: AT }));
    expect(activeAttention(job)).toHaveLength(0);
    expect(needsHuman(job)).toBe(false);

    // Сервер, который не ставит waiting_user: блок живёт, пока за ним ничего не пришло.
    let silent = applyEvent(
      initialJob("K"),
      ev("status", { state: "running", stage: "Вхожу", startedAt: AT }),
    );
    silent = applyEvent(
      silent,
      ev("block", { type: "wait", id: ID(3), at: AT, message: "Подтвердите вход" }),
    );
    expect(activeAttention(silent)).toHaveLength(1);
    silent = applyEvent(silent, ev("block", input(4)));
    expect(activeAttention(silent)).toHaveLength(0);
  });

  it("неотвеченный ответ в состоянии «отправляется» всё ещё считается вопросом", () => {
    let job = applyEvent(initialJob("J"), ev("block", input(1)));
    job = { ...job, answers: { [ID(1)]: { status: "sending", shown: null, error: null } } };
    expect(pendingQuestions(job)).toHaveLength(1);
  });
});

describe("шаги для ThinkingStream", () => {
  it("ok → done, последний бегущий шаг при waiting_user → waiting", () => {
    let job = initialJob("J");
    job = applyEvent(job, ev("block", thinking(1, "Ищу кабинет", "ok")));
    job = applyEvent(job, ev("block", thinking(2, "Читаю список курсов", "running")));
    job = applyEvent(job, ev("status", { state: "waiting_user", stage: "Жду ответа", startedAt: AT }));
    expect(thinkingSteps(job).map((step) => step.state)).toEqual(["done", "waiting"]);
    job = applyEvent(job, ev("status", { state: "running", stage: "Читаю", startedAt: AT }));
    expect(thinkingSteps(job).map((step) => step.state)).toEqual(["done", "running"]);
  });

  it("после завершения бегущие шаги закрываются: done — выполнено, failed — упало, cancelled — пропущено", () => {
    const running = applyEvent(initialJob("J"), ev("block", thinking(1, "Читаю", "running")));
    expect(thinkingSteps(applyEvent(running, ev("done", { result: {} }))).map((s) => s.state)).toEqual([
      "done",
    ]);
    expect(
      thinkingSteps(applyEvent(running, ev("error", { code: "x", detail: "" }))).map((s) => s.state),
    ).toEqual(["failed"]);
    expect(
      thinkingSteps(
        applyEvent(running, ev("status", { state: "cancelled", stage: "Отменено", startedAt: AT })),
      ).map((s) => s.state),
    ).toEqual(["skipped"]);
  });

  it("failed и skipped из потока остаются как есть, детали сохраняются", () => {
    let job = applyEvent(
      initialJob("J"),
      ev("block", { ...thinking(1, "Вход", "failed"), detail: "Не принял пароль" }),
    );
    job = applyEvent(job, ev("block", thinking(2, "Календарь", "skipped")));
    expect(thinkingSteps(job).map((step) => [step.state, step.detail])).toEqual([
      ["failed", "Не принял пароль"],
      ["skipped", undefined],
    ]);
  });
});
