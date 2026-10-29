/**
 * Разбор блоков интерфейса агента (`contracts/agent-ui.schema.json`, ADR-0004). Блок приходит JSON'ом
 * извне, поэтому каждое поле проверяется. Неизвестный тип или битый блок не игнорируется и не исполняется:
 * он превращается в `UnknownBlock` и показывается текстом с пометкой.
 */
import type { AgentUiBlock, Text } from "@/api/agent-ui.gen";
import {
  type AttachmentBlock,
  MAX_QUICK_REPLIES,
  MAX_QUICK_REPLY_CHARS,
  type PlanBlock,
  type TaskBlock,
} from "@/api/v3";

/** Текст ответа: в v3 к нему могут прилагаться быстрые ответы (кнопки под последним ответом). */
export type TextBlock = Text & { quickReplies?: string[] };

/** Блоки контракта: схема `agent-ui` плюс `plan`, `attachment`, `task` и быстрые ответы v3 (§3.3). */
export type KnownBlock =
  | Exclude<AgentUiBlock, { type: "text" }>
  | TextBlock
  | PlanBlock
  | AttachmentBlock
  | TaskBlock;
export type BlockType = KnownBlock["type"];

export interface UnknownBlock {
  type: "unknown";
  id: string;
  at: string | null;
  /** Тип, который прислал сервер (или «битый» известный). */
  rawType: string;
  /** Читаемый текст из блока, если в нём нашлось подходящее текстовое поле. */
  text: string | null;
}

export type ParsedBlock = KnownBlock | UnknownBlock;

export const KNOWN_TYPES: readonly BlockType[] = [
  "text",
  "thinking",
  "choice",
  "input",
  "confirm",
  "summary",
  "approval",
  "login",
  "wait",
  "handoff",
  "plan",
  "attachment",
  "task",
];

type Rec = Record<string, unknown>;

const isRecord = (value: unknown): value is Rec =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const isStr = (value: unknown): value is string => typeof value === "string";
const isBool = (value: unknown): value is boolean => typeof value === "boolean";
const optStr = (value: unknown) => value === undefined || isStr(value);
const optBool = (value: unknown) => value === undefined || isBool(value);
const ULID = /^[0-9A-HJKMNP-TV-Z]{26}$/;
const PLAN_STATES = ["pending", "active", "done", "skipped"];
const ATTACHMENT_KINDS = ["image", "document", "audio", "other"];
const TASK_KIND_NAMES = ["connect", "repair", "sync", "ingest"];
const isId = (value: unknown): value is string => isStr(value) && ULID.test(value);

const SOURCE_OK = (value: unknown) =>
  value === undefined ||
  (Array.isArray(value) &&
    value.length <= 10 &&
    value.every(
      (item) =>
        isRecord(item) &&
        isStr(item.title) &&
        isStr(item.url) &&
        isStr(item.fetchedAt) &&
        optBool(item.stale),
    ));

const INPUT_KINDS = ["text", "url", "number", "date", "email", "password", "code"];
const AUTOCOMPLETE = ["off", "username", "email", "current-password", "one-time-code", "url"];

/** Проверка формы известных блоков по схеме. Возвращает true, только если блок можно рисовать без догадок. */
function isValidKnown(o: Rec): boolean {
  if (!isId(o.id) || !isStr(o.at)) return false;
  switch (o.type) {
    case "text":
      return isStr(o.markdown) && SOURCE_OK(o.sources);
    case "thinking":
      return (
        isStr(o.stage) &&
        optStr(o.detail) &&
        (o.state === undefined || ["running", "ok", "failed", "skipped"].includes(o.state as string))
      );
    case "choice":
      return (
        isStr(o.question) &&
        (o.mode === "single" || o.mode === "multi") &&
        isBool(o.required) &&
        optBool(o.allowOther) &&
        Array.isArray(o.options) &&
        o.options.length >= 2 &&
        o.options.every((opt) => isRecord(opt) && isStr(opt.value) && isStr(opt.label) && optStr(opt.hint))
      );
    case "input":
      return (
        isStr(o.question) &&
        INPUT_KINDS.includes(o.inputKind as string) &&
        isBool(o.required) &&
        optStr(o.placeholder) &&
        optBool(o.secret) &&
        (o.autocomplete === undefined || AUTOCOMPLETE.includes(o.autocomplete as string)) &&
        (o.maxLength === undefined || (typeof o.maxLength === "number" && o.maxLength >= 1))
      );
    case "confirm":
      return isStr(o.question) && isStr(o.confirmLabel) && isStr(o.cancelLabel);
    case "summary":
      return (
        isStr(o.title) &&
        Array.isArray(o.groups) &&
        o.groups.every(
          (group) =>
            isRecord(group) &&
            isStr(group.title) &&
            Array.isArray(group.items) &&
            group.items.every(
              (item) =>
                isRecord(item) &&
                isStr(item.label) &&
                isStr(item.value) &&
                optStr(item.sourceUrl) &&
                (item.confidence === undefined ||
                  item.confidence === "confirmed" ||
                  item.confidence === "guess"),
            ),
        )
      );
    case "approval": {
      const p = o.proposal;
      if (!isRecord(p) || !isId(p.id) || !isStr(p.intent) || !isBool(p.reversible) || !isStr(p.expiresAt))
        return false;
      const r = p.request;
      return (
        isRecord(r) &&
        ["POST", "PUT", "PATCH"].includes(r.method as string) &&
        isStr(r.url) &&
        ["form", "json", "multipart"].includes(r.bodyKind as string) &&
        Array.isArray(r.fields) &&
        r.fields.every(
          (f) => isRecord(f) && isStr(f.name) && isStr(f.label) && isStr(f.value) && isBool(f.editable),
        )
      );
    }
    case "login":
      return (
        isId(o.connectionId) && isStr(o.ticket) && isStr(o.wsUrl) && isStr(o.expiresAt) && optStr(o.hint)
      );
    case "wait":
      return isStr(o.message) && optStr(o.expiresAt);
    case "handoff":
      return isStr(o.title) && isStr(o.url) && isStr(o.text);
    case "plan":
      return (
        isStr(o.title) &&
        Array.isArray(o.steps) &&
        o.steps.length >= 1 &&
        o.steps.length <= 20 &&
        o.steps.every(
          (step) =>
            isRecord(step) &&
            isStr(step.id) &&
            isStr(step.title) &&
            PLAN_STATES.includes(step.state as string),
        )
      );
    case "attachment":
      return (
        isStr(o.attachmentId) &&
        isStr(o.name) &&
        isStr(o.mime) &&
        typeof o.size === "number" &&
        ATTACHMENT_KINDS.includes(o.kind as string)
      );
    case "task":
      return (
        isStr(o.jobId) &&
        TASK_KIND_NAMES.includes(o.kind as string) &&
        isStr(o.title) &&
        isStr(o.state) &&
        isStr(o.threadId) &&
        (o.connectionId === undefined || isStr(o.connectionId))
      );
    default:
      return false;
  }
}

const TEXT_FIELDS = ["markdown", "text", "message", "question", "title", "stage", "detail"];

function unknownFrom(o: Rec, fallbackId: string): UnknownBlock {
  const text = TEXT_FIELDS.map((key) => o[key]).find(
    (value): value is string => isStr(value) && value.trim().length > 0,
  );
  return {
    type: "unknown",
    id: isStr(o.id) && o.id ? o.id : fallbackId,
    at: isStr(o.at) ? o.at : null,
    rawType: isStr(o.type) ? o.type : "без типа",
    text: text ? text.slice(0, 2000) : null,
  };
}

/**
 * Любой JSON → блок. Не объект — `null` (в поток такое попасть не должно, событие игнорируется).
 * Известный тип с неверной формой тоже становится `UnknownBlock`, чтобы ничего не терялось молча.
 */
export function parseBlock(raw: unknown, fallbackId = "unknown"): ParsedBlock | null {
  if (!isRecord(raw)) return null;
  if (isStr(raw.type) && (KNOWN_TYPES as readonly string[]).includes(raw.type) && isValidKnown(raw)) {
    return (raw.type === "text" ? withQuickReplies(raw) : raw) as unknown as KnownBlock;
  }
  return unknownFrom(raw, fallbackId);
}

/** Быстрые ответы: до четырёх непустых строк не длиннее 60 знаков; всё остальное молча отбрасывается. */
export function sanitizeQuickReplies(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const options = value
    .filter((item): item is string => isStr(item))
    .map((item) => item.trim())
    .filter((item) => item.length > 0 && item.length <= MAX_QUICK_REPLY_CHARS)
    .slice(0, MAX_QUICK_REPLIES);
  return options.length > 0 ? options : undefined;
}

function withQuickReplies(raw: Rec): Rec {
  if (!("quickReplies" in raw)) return raw;
  const { quickReplies, ...rest } = raw;
  const options = sanitizeQuickReplies(quickReplies);
  return options ? { ...rest, quickReplies: options } : rest;
}

/** Применяет `block_update`: поверхностное слияние патча и повторная проверка формы. */
export function patchBlock(current: ParsedBlock, patch: unknown): ParsedBlock {
  if (!isRecord(patch)) return current;
  if (current.type === "unknown") return current;
  // Тип и идентификатор патчем не меняются.
  const { type: _type, id: _id, ...rest } = patch;
  return parseBlock({ ...current, ...rest }, current.id) ?? current;
}

/** Блок, на который ждут ответа человека. */
export function isQuestion(block: ParsedBlock): boolean {
  return (
    block.type === "choice" || block.type === "input" || block.type === "confirm" || block.type === "approval"
  );
}
