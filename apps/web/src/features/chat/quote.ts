/**
 * Цитата вопроса для ответа человека. Кнопка быстрого ответа отправляет только свой текст («Да»); чтобы в ленте было
 * видно, на что отвечено, вместе с ним уходит цитата — вопрос агента простым текстом, не длиннее `QUOTE_MAX`.
 */
export const QUOTE_MAX = 200;

export interface MessageQuote {
  text: string;
  /** Блок, который цитируется, если он известен. */
  blockId?: string;
}

/** Markdown → одна строка простого текста: без разметки, ссылок, кода и таблиц. */
export function plainText(markdown: string): string {
  return markdown
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/^\s*\|.*\|\s*$/gm, " ")
    .replace(/^\s{0,3}(?:#{1,6}|>|[-*+]|\d+[.)])\s+/gm, "")
    .replace(/[*`]+/g, "")
    .replace(/(^|\s)_+([^\s_](?:[^_\n]*[^\s_])?)_+(?=\s|$|[.,;:!?])/g, "$1$2")
    .replace(/\s+/g, " ")
    .trim();
}

const clip = (text: string): string =>
  text.length <= QUOTE_MAX ? text : `${text.slice(0, QUOTE_MAX - 1).trimEnd()}…`;

/**
 * Что процитировать из реплики агента: его вопрос. Это последняя фраза со знаком вопроса в последнем абзаце,
 * а если вопроса нет — последняя фраза. Длинное обрезается до `QUOTE_MAX` с многоточием.
 */
export function quoteOf(markdown: string, blockId?: string): MessageQuote | null {
  const paragraphs = markdown
    .split(/\n\s*\n/)
    .map(plainText)
    .filter(Boolean);
  const last = paragraphs.at(-1);
  if (!last) return null;
  const sentences = last.split(/(?<=[.!?…])\s+/).filter(Boolean);
  const text = sentences.findLast((sentence) => sentence.endsWith("?")) ?? sentences.at(-1) ?? last;
  return { text: clip(text), ...(blockId ? { blockId } : {}) };
}
