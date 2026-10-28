/**
 * Симулятор задания для мок-режима: ведёт журнал событий потока (contracts/events.md §1), ждёт ответов на блоки
 * и отдаёт события по `Last-Event-ID`. Секреты в журнал не попадают — как у настоящего сервера.
 */
import type { AgentUiBlock } from "@/api/agent-ui.gen";

export interface SimEvent {
  id: number;
  event: string;
  data: unknown;
}

type JobState = "queued" | "running" | "waiting_user" | "done" | "failed" | "cancelled";

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

export class JobCancelledError extends Error {}

export class JobSim {
  readonly events: SimEvent[] = [];
  state: JobState = "queued";
  startedAt = 0;
  private nextId = 1;
  private readonly listeners = new Set<() => void>();
  private readonly waiting = new Map<string, (values: unknown) => void>();
  private readonly answered = new Set<string>();
  /** Значения секретных ответов хранятся только здесь (имитация Vault) и в журнал не пишутся. */
  readonly sealed = new Map<string, unknown>();
  cancelled = false;
  /** Fixture projection is written before SSE listeners see the event, like durable server turns. */
  onEvent?: (event: SimEvent) => void;

  constructor(
    readonly jobId: string,
    private readonly now: () => number,
    private readonly pace: number,
    public connectionId: string | null = null,
    readonly kind = "chat",
  ) {}

  /** Пауза сценария; при `pace = 0` — без ожидания (тесты). */
  async pause(ms: number): Promise<void> {
    if (this.cancelled) throw new JobCancelledError();
    if (this.pace > 0) await sleep(ms * this.pace);
    if (this.cancelled) throw new JobCancelledError();
  }

  emit(event: string, data: unknown): void {
    // Конец задания фиксируется вместе с событием: поток закрывается только после его доставки.
    if (event === "done") this.state = "done";
    if (event === "error") this.state = "failed";
    const next = { id: this.nextId++, event, data };
    this.events.push(next);
    this.onEvent?.(next);
    for (const listener of this.listeners) listener();
  }

  status(stage: string, state: JobState = "running"): void {
    this.state = state;
    if (!this.startedAt) this.startedAt = this.now();
    this.emit("status", {
      state,
      stage,
      startedAt: new Date(this.startedAt).toISOString(),
      elapsedMs: this.now() - this.startedAt,
    });
  }

  block(block: AgentUiBlock): void {
    this.emit("block", block);
  }

  update(id: string, patch: Record<string, unknown>): void {
    this.emit("block_update", { id, patch });
  }

  /** Задаёт вопрос блоком и ждёт ответа; состояние задания — `waiting_user`. */
  async ask(block: AgentUiBlock, stage: string): Promise<unknown> {
    const answer = new Promise<unknown>((resolve) => this.waiting.set(block.id, resolve));
    this.block(block);
    this.status(stage, "waiting_user");
    const values = await answer;
    if (this.cancelled) throw new JobCancelledError();
    this.status(stage, "running");
    return values;
  }

  /** Ответ человека. `false` — такого вопроса нет, `"answered"` — уже отвечен (409). */
  answer(blockId: string, values: unknown, secret: boolean): boolean | "answered" {
    if (this.answered.has(blockId)) return "answered";
    const resolve = this.waiting.get(blockId);
    if (!resolve) return false;
    this.answered.add(blockId);
    this.waiting.delete(blockId);
    if (secret) this.sealed.set(blockId, values);
    resolve(values);
    return true;
  }

  cancel(): void {
    if (this.state === "done" || this.state === "failed" || this.state === "cancelled") return;
    this.cancelled = true;
    this.state = "cancelled";
    this.emit("status", {
      state: "cancelled",
      stage: "Отменено",
      startedAt: new Date(this.startedAt || this.now()).toISOString(),
      elapsedMs: this.now() - (this.startedAt || this.now()),
    });
    for (const resolve of this.waiting.values()) resolve(null);
    this.waiting.clear();
  }

  get terminal(): boolean {
    return this.state === "done" || this.state === "failed" || this.state === "cancelled";
  }

  get pendingIds(): readonly string[] {
    return [...this.waiting.keys()];
  }
  get answeredIds(): readonly string[] {
    return [...this.answered];
  }

  /** Поток SSE: события после `lastEventId`, затем новые, пока задание не закончится. */
  stream(lastEventId: number): ReadableStream<Uint8Array> {
    const encoder = new TextEncoder();
    let cursor = lastEventId;
    let unsubscribe = () => {};
    return new ReadableStream<Uint8Array>({
      start: (controller) => {
        const flush = () => {
          for (const event of this.events) {
            if (event.id <= cursor) continue;
            cursor = event.id;
            controller.enqueue(
              encoder.encode(
                `id: ${event.id}\nevent: ${event.event}\ndata: ${JSON.stringify(event.data)}\n\n`,
              ),
            );
          }
          if (this.terminal && cursor >= (this.events.at(-1)?.id ?? 0)) {
            unsubscribe();
            try {
              controller.close();
            } catch {
              /* поток уже закрыт клиентом */
            }
          }
        };
        this.listeners.add(flush);
        unsubscribe = () => this.listeners.delete(flush);
        controller.enqueue(encoder.encode(":keepalive\n\n"));
        flush();
      },
      cancel: () => unsubscribe(),
    });
  }
}
