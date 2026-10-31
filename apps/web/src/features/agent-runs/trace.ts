import { DEFAULT_TIME_ZONE, formatDuration, formatTime, type IconName, NBSP, pluralRu } from "@botai/design";
import type { AgentRun, AgentRunEvent } from "@/api/agent-runs";

/**
 * Словарь трассы агента: состояния хода, виды шагов и разбор payload'ов
 * (`backend/src/botai/agent/v4/runtime.py`, `contracts/run.schema.json`).
 *
 * Цвет здесь — не единственный сигнал: у каждого состояния есть слово, иконка и объяснение.
 * Технические коды показываются только в трассе для владельца и всегда рядом с человеческим текстом.
 */
export type RunTone = "success" | "info" | "warning" | "danger" | "neutral";

export interface RunStatusView {
  label: string;
  detail: string;
  tone: RunTone;
  icon: IconName;
  /** Ход ещё идёт: счётчики и шаги обновляются в фоне. */
  active: boolean;
}

const RUN_STATUS: Record<string, RunStatusView> = {
  running: {
    label: "Выполняется",
    detail: "Агент работает: шаги трассы появляются по мере выполнения.",
    tone: "info",
    icon: "sync",
    active: true,
  },
  answer: {
    label: "Завершено",
    detail: "Ход закончен ответом агента.",
    tone: "success",
    icon: "ok",
    active: false,
  },
  paused: {
    label: "Пауза",
    detail: "Ход остановлен на паузе и ждёт продолжения.",
    tone: "warning",
    icon: "paused",
    active: false,
  },
  cancelled: {
    label: "Отменено",
    detail: "Ход отменён, работа остановлена.",
    tone: "neutral",
    icon: "paused",
    active: false,
  },
  step_limit: {
    label: "Остановлено лимитом шагов",
    detail: "Достигнут предел числа шагов хода. Сделанное сохранено в трассе.",
    tone: "warning",
    icon: "warning",
    active: false,
  },
  time_limit: {
    label: "Остановлено лимитом времени",
    detail: "Ход остановлен по времени. Сделанное сохранено в трассе.",
    tone: "warning",
    icon: "warning",
    active: false,
  },
  token_limit: {
    label: "Остановлено лимитом токенов",
    detail: "Ход остановлен по лимиту токенов. Сделанное сохранено в трассе.",
    tone: "warning",
    icon: "warning",
    active: false,
  },
  cost_limit: {
    label: "Остановлено мягким лимитом стоимости",
    detail: "Достигнут мягкий лимит стоимости хода. Продолжение — отдельным решением.",
    tone: "warning",
    icon: "warning",
    active: false,
  },
  hard_stop: {
    label: "Остановлено жёстким лимитом",
    detail: "Жёсткий лимит стоимости остановил ход. Сделанное сохранено в трассе.",
    tone: "danger",
    icon: "error",
    active: false,
  },
  error: {
    label: "Сбой",
    detail: "Ход завершился ошибкой: подробности — в шаге «Ошибка».",
    tone: "danger",
    icon: "error",
    active: false,
  },
};

const UNKNOWN_STATUS: RunStatusView = {
  label: "Неизвестное состояние",
  detail: "Сервер вернул состояние, которого интерфейс ещё не знает.",
  tone: "neutral",
  icon: "connection",
  active: false,
};

/** Состояние хода человеческим языком; неизвестное значение не ломает экран. */
export function describeRunStatus(status: string): RunStatusView {
  return Object.hasOwn(RUN_STATUS, status) ? (RUN_STATUS[status] ?? UNKNOWN_STATUS) : UNKNOWN_STATUS;
}

export interface EventKindView {
  label: string;
  icon: IconName;
}

const EVENT_KIND: Record<string, EventKindView> = {
  run_started: { label: "Запуск", icon: "source" },
  model_call: { label: "Ответ модели", icon: "chat" },
  tool_call: { label: "Вызов инструмента", icon: "connection" },
  tool_result: { label: "Результат инструмента", icon: "check" },
  limit: { label: "Лимит", icon: "warning" },
  pause: { label: "Пауза", icon: "paused" },
  cancel: { label: "Отмена", icon: "close" },
  error: { label: "Ошибка", icon: "error" },
  note: { label: "Заметка", icon: "info" },
  run_finished: { label: "Итог", icon: "ok" },
};

const UNKNOWN_EVENT: EventKindView = { label: "Шаг", icon: "info" };

export function describeEventKind(kind: string): EventKindView {
  return Object.hasOwn(EVENT_KIND, kind) ? (EVENT_KIND[kind] ?? UNKNOWN_EVENT) : UNKNOWN_EVENT;
}

export type JsonRecord = Record<string, unknown>;

export function asRecord(value: unknown): JsonRecord | null {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as JsonRecord) : null;
}

export function asText(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

export function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function asInt(value: unknown): number | null {
  const number = asNumber(value);
  return number === null ? null : Math.trunc(number);
}

export function asBool(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

export function asList(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

const LIMIT_LABEL: Record<string, string> = {
  steps: "шаги",
  seconds: "время",
  tokens: "токены",
  usd: "стоимость",
  hard_stop_usd: "жёсткий лимит стоимости",
};

/** Причина лимита словами; неизвестная не притворяется известной. */
export function limitLabel(raw: unknown): string {
  const text = asText(raw);
  if (!text) return "неизвестный лимит";
  return LIMIT_LABEL[text] ?? "неизвестный лимит";
}

/** «12 345 токенов» — число с разрядами и неразрывным пробелом, как в styleguide §9. */
export function formatTokens(tokens: number): string {
  const grouped = new Intl.NumberFormat("ru-RU").format(tokens);
  return `${grouped}${NBSP}${pluralRu(tokens, ["токен", "токена", "токенов"])}`;
}

/** Стоимость как её посчитал сервер: в долларах; малые суммы — с четырьмя знаками. */
export function formatCostUsd(usd: number, locale = "ru-RU"): string {
  const fractionDigits = usd > 0 && usd < 0.01 ? 4 : 2;
  return new Intl.NumberFormat(locale, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  }).format(usd);
}

/** «1 мин 5 с» из настоящей длительности; без часов, если их нет. */
export function formatRunDuration(ms: number): string {
  return formatDuration(Math.max(0, ms));
}

/** Сколько шёл ход: до конца у завершённого, до «сейчас» у идущего (время передаётся, не берётся из часов). */
export function runDurationMs(run: Pick<AgentRun, "startedAt" | "finishedAt">, nowMs: number): number | null {
  if (!run.startedAt) return null;
  const started = Date.parse(run.startedAt);
  const ended = run.finishedAt ? Date.parse(run.finishedAt) : nowMs;
  if (!Number.isFinite(started) || !Number.isFinite(ended)) return null;
  return Math.max(0, ended - started);
}

/** Время шага в поясе приложения: «14:05». */
export function eventTime(event: Pick<AgentRunEvent, "at">, timeZone = DEFAULT_TIME_ZONE): string {
  const at = Date.parse(event.at);
  return Number.isFinite(at) ? formatTime(at, timeZone) : "";
}

/** Строка шага: «#4 · 14:05 · 1 мин 5 с» — части, которых сервер не дал, не выдумываются. */
export function eventCaption(event: AgentRunEvent, timeZone = DEFAULT_TIME_ZONE): string {
  const parts: string[] = [];
  if (event.step > 0) parts.push(`#${event.step}`);
  const time = eventTime(event, timeZone);
  if (time) parts.push(time);
  if (event.durationMs > 0) parts.push(formatRunDuration(event.durationMs));
  return parts.join(" · ");
}

/** Заголовок шага: вид + то, что отличает его от соседей (имя инструмента, модель). */
export function eventHeadline(event: AgentRunEvent): string {
  const payload = asRecord(event.payload) ?? {};
  const view = describeEventKind(event.kind);
  if (event.kind === "tool_call" || event.kind === "tool_result") {
    const tool = asText(payload.tool);
    return tool ? `${view.label}: ${tool}` : view.label;
  }
  if (event.kind === "model_call") {
    const model = asText(payload.model);
    return model ? `${view.label}: ${model}` : view.label;
  }
  if (event.kind === "limit") return `Лимит: ${limitLabel(payload.limit)}`;
  return view.label;
}

/** Короткая суть шага для свёрнутой строки: только то, что сервер действительно прислал. */
export function eventSummary(event: AgentRunEvent): string | null {
  const payload = asRecord(event.payload) ?? {};
  switch (event.kind) {
    case "tool_result":
      return asText(payload.summary);
    case "error":
      return asText(payload.detail) ?? "Шаг завершился ошибкой.";
    case "pause":
    case "cancel": {
      const reason = asText(payload.reason);
      return reason === "control" ? "Остановлено владельцем хода." : null;
    }
    case "limit": {
      const steps = asInt(payload.steps);
      const tokens = asInt(payload.total_tokens);
      const cost = asNumber(payload.cost_usd);
      const parts: string[] = [];
      if (steps !== null) parts.push(`шагов: ${steps}`);
      if (tokens !== null) parts.push(formatTokens(tokens));
      if (cost !== null) parts.push(formatCostUsd(cost));
      return parts.length > 0 ? parts.join(" · ") : null;
    }
    case "run_finished": {
      const status = asText(payload.status);
      return status ? describeRunStatus(status).label : null;
    }
    default:
      return null;
  }
}

const ARTIFACT_LABEL: Record<string, string> = {
  page: "Страница",
  json: "Данные",
  file: "Файл",
  doc: "Документ",
  har: "Запись сети",
  screenshot: "Снимок экрана",
  log: "Журнал",
  other: "Материал",
};

/** Вид артефакта словами; неизвестный вид — нейтральный, без английского имени с сервера. */
export function artifactLabel(kind: string): string {
  return ARTIFACT_LABEL[kind] ?? "Материал";
}

const PURPOSE_LABEL: Record<string, string> = {
  agent: "задача агента",
  chat: "чат",
};

/** Назначение хода словами; неизвестное оставляем как есть — это диагностика владельца. */
export function purposeLabel(raw: string): string {
  return PURPOSE_LABEL[raw] ?? raw;
}

const LIMIT_KEY_LABEL: Record<string, string> = {
  max_steps: "шаги",
  max_seconds: "время",
  max_tokens: "токены",
  max_usd: "стоимость",
  hard_stop_usd: "жёсткий лимит",
};

/** Предел хода словами; неизвестный ключ не прячем — владелец видит настоящий. */
export function limitKeyLabel(key: string): string {
  return LIMIT_KEY_LABEL[key] ?? key;
}

export interface ArgumentRow {
  name: string;
  value: string;
}

/** Аргументы вызова для карточки шага: простые значения — строками, вложенные — компактным JSON. */
export function argumentRows(value: unknown): ArgumentRow[] {
  const record = asRecord(value);
  if (!record) return [];
  return Object.entries(record).map(([name, raw]) => ({
    name,
    value:
      typeof raw === "string"
        ? raw
        : typeof raw === "number" || typeof raw === "boolean" || raw === null
          ? String(raw)
          : JSON.stringify(raw, null, 2),
  }));
}

export interface RunArtifact {
  id: string;
  kind: string;
  bytes: number | null;
  url: string | null;
}

/**
 * Артефакты, которые сервер сохранил в шагах `tool_result` (`result.artifacts`). Это и есть
 * «что прочитано» без отдельного workspace-API; дубликаты по id схлопываются.
 */
export function artifactsFromEvents(events: readonly AgentRunEvent[]): RunArtifact[] {
  const seen = new Map<string, RunArtifact>();
  for (const event of events) {
    if (event.kind !== "tool_result") continue;
    const payload = asRecord(event.payload) ?? {};
    const result = asRecord(payload.result) ?? {};
    for (const item of asList(result.artifacts)) {
      const artifact = asRecord(item);
      if (!artifact) continue;
      const id = asText(artifact.id);
      const kind = asText(artifact.kind);
      if (!id || !kind) continue;
      const previous = seen.get(id);
      seen.set(id, {
        id,
        kind,
        bytes: asInt(artifact.bytes) ?? previous?.bytes ?? null,
        url: asText(artifact.url) ?? previous?.url ?? null,
      });
    }
  }
  return [...seen.values()];
}

/** Содержимое ответа модели (уже маскировано сервером) — текст, не разметка. */
export function modelContent(event: AgentRunEvent): string | null {
  const payload = asRecord(event.payload);
  return payload ? asText(payload.content) : null;
}

/** Полный результат инструмента из трассы — компактный текст для владельца. */
export function toolResultText(event: AgentRunEvent): string | null {
  const payload = asRecord(event.payload);
  if (!payload) return null;
  const result = payload.result;
  if (result === undefined || result === null) return null;
  try {
    return JSON.stringify(result, null, 2);
  } catch {
    return null;
  }
}

/** Ошибка шага: человеческий текст + код отдельно. */
export function eventError(event: AgentRunEvent): { code: string | null; detail: string | null } {
  const payload = asRecord(event.payload) ?? {};
  return { code: asText(payload.code), detail: asText(payload.detail) };
}
