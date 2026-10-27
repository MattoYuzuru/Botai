import { useSyncExternalStore } from "react";
import { api, call } from "@/api/client";
import { humanMessage, isApiError } from "@/api/errors";
import { fieldsHash } from "@/lib/hash";
import { type AnswerState, applyEvent, initialJob, isTerminal, type JobSnapshot } from "./job-state";
import { readSse, reconnectDelay, SseHttpError } from "./sse";

type Listener = () => void;

/** Ответ на блок: строка, список строк (multi) или да/нет (confirm). */
export type AnswerValues = string | string[] | boolean;

/** Если из потока ничего не приходит (даже heartbeat раз в 15 с) дольше этого, соединение считается зависшим. */
const SILENCE_MS = 45_000;

const wait = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve) => {
    const done = () => {
      clearTimeout(timer);
      window.removeEventListener("online", done);
      signal.removeEventListener("abort", done);
      resolve();
    };
    // Вернулась сеть — не ждём остаток паузы.
    const timer = setTimeout(done, ms);
    window.addEventListener("online", done);
    signal.addEventListener("abort", done);
  });

/**
 * Потоки заданий живут вне компонентов: свёрнутый `ProgressWall` не теряет связь, а вопрос агента виден
 * в шапке экранов. Один поток на задание; переподключение по `Last-Event-ID` идёт в фоне с нарастающей
 * паузой, без кнопки «Повторить».
 */
export class JobManager {
  private readonly jobs = new Map<string, JobSnapshot>();
  private readonly controllers = new Map<string, AbortController>();
  private readonly listeners = new Set<Listener>();
  /** Версия для `useSyncExternalStore`: меняется при любом изменении любого задания. */
  private version = 0;

  constructor(
    private readonly origin: () => string = () => window.location.origin,
    private readonly backoff: (attempt: number) => number = reconnectDelay,
  ) {}

  get(jobId: string): JobSnapshot | undefined {
    return this.jobs.get(jobId);
  }

  subscribe = (listener: Listener) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  getVersion = () => this.version;

  private set(jobId: string, update: (job: JobSnapshot) => JobSnapshot) {
    const current = this.jobs.get(jobId);
    if (!current) return;
    const next = update(current);
    if (next === current) return;
    this.jobs.set(jobId, next);
    this.version += 1;
    for (const listener of this.listeners) listener();
  }

  /** Запускает поток задания; повторный вызов ничего не делает. */
  start(jobId: string): void {
    if (this.controllers.has(jobId)) return;
    const controller = new AbortController();
    this.controllers.set(jobId, controller);
    if (!this.jobs.has(jobId)) {
      this.jobs.set(jobId, initialJob(jobId));
      this.version += 1;
      for (const listener of this.listeners) listener();
    }
    void this.run(jobId, controller);
  }

  /**
   * Кладёт готовый снимок задания без сети: витрина `/styleguide` и тесты показывают экран в нужном состоянии.
   * Поток для такого задания не запускается.
   */
  seed(snapshot: JobSnapshot): void {
    this.controllers.set(snapshot.jobId, new AbortController());
    this.jobs.set(snapshot.jobId, snapshot);
    this.version += 1;
    for (const listener of this.listeners) listener();
  }

  /** Останавливает поток и забывает задание (после «Готово» или при выходе из аккаунта). */
  stop(jobId: string): void {
    this.controllers.get(jobId)?.abort();
    this.controllers.delete(jobId);
    if (this.jobs.delete(jobId)) {
      this.version += 1;
      for (const listener of this.listeners) listener();
    }
  }

  stopAll(): void {
    for (const jobId of [...this.jobs.keys()]) this.stop(jobId);
  }

  private async run(jobId: string, controller: AbortController): Promise<void> {
    const { signal } = controller;
    let attempt = 0;
    while (!signal.aborted) {
      const before = this.jobs.get(jobId);
      if (!before) return;
      this.set(jobId, (job) => ({
        ...job,
        link: job.lastEventId === null && attempt === 0 ? "connecting" : "reconnecting",
      }));

      // Сторож зависания: если поток молчит, обрываем только эту попытку и переподключаемся.
      const attemptController = new AbortController();
      const abortAttempt = () => attemptController.abort();
      signal.addEventListener("abort", abortAttempt);
      let watchdog = setTimeout(abortAttempt, SILENCE_MS);
      const touch = () => {
        clearTimeout(watchdog);
        watchdog = setTimeout(abortAttempt, SILENCE_MS);
      };

      try {
        await readSse({
          url: `${this.origin()}/api/v1/jobs/${encodeURIComponent(jobId)}/events`,
          signal: attemptController.signal,
          lastEventId: this.jobs.get(jobId)?.lastEventId ?? undefined,
          onActivity: touch,
          onOpen: () => {
            attempt = 0;
            this.set(jobId, (job) => ({ ...job, link: "live" }));
          },
          onMessage: (message) =>
            this.set(jobId, (job) => {
              const next = applyEvent(job, message);
              // Итог получен — поток больше не нужен, связь считается закрытой сразу.
              return isTerminal(next.state) ? { ...next, link: "closed" } : next;
            }),
        });
      } catch (error) {
        if (signal.aborted) return;
        if (
          error instanceof SseHttpError &&
          error.status >= 400 &&
          error.status < 500 &&
          error.status !== 429
        ) {
          this.fail(jobId, error);
          return;
        }
        if (error instanceof SseHttpError && error.status === 501) {
          this.fail(jobId, error);
          return;
        }
      } finally {
        clearTimeout(watchdog);
        signal.removeEventListener("abort", abortAttempt);
      }

      if (signal.aborted) return;
      const job = this.jobs.get(jobId);
      if (!job) return;
      if (isTerminal(job.state)) {
        this.set(jobId, (current) => ({ ...current, link: "closed" }));
        return;
      }
      this.set(jobId, (current) => ({ ...current, link: "reconnecting" }));
      await wait(this.backoff(attempt), signal);
      attempt += 1;
    }
  }

  /** Сервер ответил, что потока не будет: задание неизвестно, чужое или ещё не поддержано. Человеку — текст, не код. */
  private fail(jobId: string, error: SseHttpError): void {
    const body =
      typeof error.body === "object" && error.body !== null ? (error.body as Record<string, unknown>) : {};
    const code = typeof body.code === "string" ? body.code : error.status === 404 ? "not_found" : "unknown";
    const detail =
      typeof body.detail === "string" && body.detail
        ? body.detail
        : error.status === 404
          ? "Задание не найдено. Возможно, оно уже завершилось."
          : error.status === 401
            ? "Нужно снова войти в BotAI."
            : "";
    this.set(jobId, (job) => ({
      ...job,
      state: "failed",
      link: "closed",
      problem: job.problem ?? { code, detail, extra: {} },
    }));
  }

  protected patchAnswer(jobId: string, blockId: string, answer: AnswerState) {
    this.set(jobId, (job) => ({ ...job, answers: { ...job.answers, [blockId]: answer } }));
  }

  /**
   * Ответ на блок: `POST /jobs/{id}/answers`. Значение не сохраняется ни в состоянии задания, ни в кэше
   * запросов: мутация React Query хранила бы переменные, поэтому вызов идёт напрямую. `shown` — что
   * показать в истории как ответ (для секрета — `null`). Повторный ответ (409) считается данным.
   */
  async answer(
    jobId: string,
    blockId: string,
    values: AnswerValues | null,
    shown: string | null,
  ): Promise<void> {
    this.patchAnswer(jobId, blockId, { status: "sending", shown, error: null });
    try {
      await call(
        api.POST("/api/v1/jobs/{job_id}/answers", {
          params: { path: { job_id: jobId } },
          body: { blockId, values },
        }),
      );
      this.patchAnswer(jobId, blockId, { status: "answered", shown, error: null });
    } catch (error) {
      if (isApiError(error) && error.code === "block_answered") {
        this.patchAnswer(jobId, blockId, { status: "answered", shown, error: null });
        return;
      }
      this.patchAnswer(jobId, blockId, { status: "failed", shown: null, error: humanMessage(error) });
    }
  }

  /**
   * Одобрение записи: `POST /actions/{id}/approve` с итоговыми полями и хешем тех, что человек видел.
   * Подтверждение одноразовое: `action_changed` — подтвердить заново, `action_expired` — ждать нового предложения.
   */
  async approve(
    jobId: string,
    blockId: string,
    proposalId: string,
    fields: Record<string, string>,
  ): Promise<void> {
    this.patchAnswer(jobId, blockId, { status: "sending", shown: null, error: null });
    try {
      // Хеш считается по полям, которые человек увидел (из блока), а не по итоговым: правки уходят в `fields`.
      const block = this.get(jobId)?.blocks.find((item) => item.id === blockId);
      const shownFields =
        block?.type === "approval"
          ? Object.fromEntries(block.proposal.request.fields.map((field) => [field.name, field.value]))
          : fields;
      await call(
        api.POST("/api/v1/actions/{proposal_id}/approve", {
          params: { path: { proposal_id: proposalId } },
          body: { fields, fieldsHash: await fieldsHash(shownFields) },
        }),
      );
      this.patchAnswer(jobId, blockId, { status: "answered", shown: null, error: null, outcome: "approved" });
    } catch (error) {
      if (isApiError(error) && error.code === "action_expired") {
        this.patchAnswer(jobId, blockId, { status: "expired", shown: null, error: null });
      } else {
        this.patchAnswer(jobId, blockId, { status: "failed", shown: null, error: humanMessage(error) });
      }
    }
  }

  async reject(jobId: string, blockId: string, proposalId: string): Promise<void> {
    this.patchAnswer(jobId, blockId, { status: "sending", shown: null, error: null });
    try {
      await call(
        api.POST("/api/v1/actions/{proposal_id}/reject", { params: { path: { proposal_id: proposalId } } }),
      );
      this.patchAnswer(jobId, blockId, { status: "answered", shown: null, error: null, outcome: "rejected" });
    } catch (error) {
      this.patchAnswer(jobId, blockId, { status: "failed", shown: null, error: humanMessage(error) });
    }
  }

  /** «Отмена» работы: сервер остановит задание, поток пришлёт итоговое состояние. */
  async cancel(jobId: string): Promise<void> {
    try {
      await call(api.POST("/api/v1/jobs/{job_id}/cancel", { params: { path: { job_id: jobId } } }));
    } catch {
      /* отмена — best effort: задание всё равно закроется по таймауту ожидания */
    }
  }
}

export const jobManager = new JobManager();

/** Снимок задания; обновляется на каждое событие потока. */
export function useJob(
  jobId: string | null | undefined,
  manager: JobManager = jobManager,
): JobSnapshot | undefined {
  useSyncExternalStore(manager.subscribe, manager.getVersion, manager.getVersion);
  return jobId ? manager.get(jobId) : undefined;
}
