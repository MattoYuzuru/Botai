/**
 * Разбор блоков интерфейса агента (`contracts/agent-ui.schema.json`, ADR-0004). Блок приходит JSON'ом
 * извне, поэтому каждое поле проверяется. Неизвестный тип или битый блок не игнорируется и не исполняется:
 * он превращается в `UnknownBlock` и показывается текстом с пометкой.
 */
import type { AgentUiBlock } from "@/api/agent-ui.gen";

export type KnownBlock = AgentUiBlock;
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
];

type Rec = Record<string, unknown>;

const isRecord = (value: unknown): value is Rec =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const isStr = (value: unknown): value is string => typeof value === "string";
const isBool = (value: unknown): value is boolean => typeof value === "boolean";
const optStr = (value: unknown) => value === undefined || isStr(value);
const optBool = (value: unknown) => value === undefined || isBool(value);
const ULID = /^[0-9A-HJKMNP-TV-Z]{26}$/;
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
    return raw as unknown as KnownBlock;
  }
  return unknownFrom(raw, fallbackId);
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
