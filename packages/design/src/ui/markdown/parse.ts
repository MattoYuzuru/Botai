/**
 * Безопасный разбор markdown ответа агента. Поддерживается узкое подмножество: абзацы, списки,
 * заголовки, **жирный**, *курсив*, `код`, блоки кода, цитаты, таблицы GFM, графики (```chart) и ссылки.
 * HTML не разбирается вовсе — теги остаются обычным текстом; картинки не загружаются (показывается подпись);
 * ссылками становятся только абсолютные адреса: http(s) и внутренние `схема://…` (их разрешает вызывающий код
 * через `resolveLink`). `javascript:`, `data:`, `mailto:` и относительные пути ссылками не становятся (ADR-0004).
 */
import { safeHttpUrl } from "../../lib/safe-url";
import { type ChartSpec, parseChartSpec } from "./chart-spec";

export type Inline =
  | { type: "text"; value: string }
  | { type: "bold"; children: Inline[] }
  | { type: "italic"; children: Inline[] }
  | { type: "code"; value: string }
  /** `href` — как в тексте: для http(s) нормализованный адрес, для внутренних (`botai://…`) исходная строка. */
  | { type: "link"; href: string; children: Inline[] };

export type TableAlign = "left" | "center" | "right" | null;

export type Block =
  | { type: "paragraph"; children: Inline[] }
  | { type: "heading"; children: Inline[] }
  | { type: "list"; ordered: boolean; items: Inline[][] }
  | { type: "quote"; children: Inline[] }
  | { type: "code"; value: string; lang?: string }
  | { type: "table"; align: TableAlign[]; header: Inline[][]; rows: Inline[][][] }
  | { type: "chart"; spec: ChartSpec };

const URL_RE = /(?:https?|botai):\/\/[^\s<>()[\]"']+[^\s<>()[\]"'.,;:!?]/;
const AUTOLINK_RE = /^<((?:https?|botai):\/\/[^\s<>]+)>/;
const CUSTOM_SCHEME_RE = /^([a-z][a-z0-9+.-]*):\/\/\S+$/i;
/** Схемы, которые не бывают внутренними ссылками приложения, даже с «//». */
const FORBIDDEN_SCHEMES = new Set(["javascript", "data", "vbscript", "file", "blob", "about"]);
const ALNUM = /[\p{L}\p{N}]/u;
const WORDY = /[\p{L}\p{N}_]/u;

/** Адрес из разметки → адрес ссылки или `null`. http(s) нормализуется, прочие абсолютные `схема://…` остаются как есть. */
function linkTarget(raw: string | undefined): string | null {
  if (!raw) return null;
  const http = safeHttpUrl(raw);
  if (http) return http.href;
  const custom = CUSTOM_SCHEME_RE.exec(raw.trim());
  if (!custom || FORBIDDEN_SCHEMES.has((custom[1] ?? "").toLowerCase()) || /^https?$/i.test(custom[1] ?? ""))
    return null;
  return raw.trim();
}

/** Разбор строки на встроенные элементы. Незакрытая разметка остаётся текстом. */
export function parseInline(source: string): Inline[] {
  const out: Inline[] = [];
  let text = "";
  const flush = () => {
    if (text) out.push({ type: "text", value: text });
    text = "";
  };
  let i = 0;
  while (i < source.length) {
    const rest = source.slice(i);
    const previous = i === 0 ? "" : (source[i - 1] ?? "");

    if (rest.startsWith("`")) {
      const end = rest.indexOf("`", 1);
      if (end > 1) {
        flush();
        out.push({ type: "code", value: rest.slice(1, end) });
        i += end + 1;
        continue;
      }
    }

    // ***жирный курсив***
    if (rest.startsWith("***")) {
      const end = rest.indexOf("***", 3);
      if (end > 3) {
        flush();
        out.push({ type: "bold", children: [{ type: "italic", children: parseInline(rest.slice(3, end)) }] });
        i += end + 3;
        continue;
      }
    }

    if (rest.startsWith("**")) {
      const end = rest.indexOf("**", 2);
      if (end > 2) {
        flush();
        out.push({ type: "bold", children: parseInline(rest.slice(2, end)) });
        i += end + 2;
        continue;
      }
    }

    // Картинка: ![alt](url) — не грузим, показываем подпись.
    const image = /^!\[([^\]]*)\]\(([^)\s]*)\)/.exec(rest);
    if (image) {
      text += image[1] ?? "";
      i += image[0].length;
      continue;
    }

    const link = /^\[([^\]]+)\]\(([^)\s]+)\)/.exec(rest);
    if (link) {
      const target = linkTarget(link[2]);
      flush();
      if (target) out.push({ type: "link", href: target, children: parseInline(link[1] ?? "") });
      else out.push({ type: "text", value: link[1] ?? "" });
      i += link[0].length;
      continue;
    }

    // <https://…> — автоссылка.
    const auto = AUTOLINK_RE.exec(rest);
    if (auto) {
      const target = linkTarget(auto[1]);
      if (target) {
        flush();
        out.push({ type: "link", href: target, children: [{ type: "text", value: auto[1] ?? "" }] });
        i += auto[0].length;
        continue;
      }
    }

    const bare = /^(?:https?|botai):\/\//.test(rest) ? URL_RE.exec(rest) : null;
    if (bare && bare.index === 0) {
      const target = linkTarget(bare[0]);
      flush();
      if (target) out.push({ type: "link", href: target, children: [{ type: "text", value: bare[0] }] });
      else out.push({ type: "text", value: bare[0] });
      i += bare[0].length;
      continue;
    }

    // Курсив: *слово* и _слово_ на границе слов. `snake_case`, `2*3*4` и `* пункт` курсивом не становятся.
    if (rest.startsWith("*") && !ALNUM.test(previous)) {
      const star = /^\*([^\s*](?:[^*\n]*[^\s*])?)\*(?![\p{L}\p{N}])/u.exec(rest);
      if (star) {
        flush();
        out.push({ type: "italic", children: parseInline(star[1] ?? "") });
        i += star[0].length;
        continue;
      }
    }
    if (rest.startsWith("_") && !WORDY.test(previous)) {
      const under = /^_([^\s_](?:[^_\n]*[^\s_])?)_(?![\p{L}\p{N}_])/u.exec(rest);
      if (under) {
        flush();
        out.push({ type: "italic", children: parseInline(under[1] ?? "") });
        i += under[0].length;
        continue;
      }
    }

    text += source[i];
    i++;
  }
  flush();
  return out;
}

/** Ячейки строки таблицы: крайние `|` необязательны, `\|` — литерал. */
function splitRow(line: string): string[] {
  let body = line.trim();
  if (body.startsWith("|")) body = body.slice(1);
  if (body.endsWith("|") && !body.endsWith("\\|")) body = body.slice(0, -1);
  const cells: string[] = [];
  let current = "";
  for (let index = 0; index < body.length; index++) {
    const char = body[index];
    if (char === "\\" && body[index + 1] === "|") {
      current += "|";
      index++;
    } else if (char === "|") {
      cells.push(current.trim());
      current = "";
    } else current += char;
  }
  cells.push(current.trim());
  return cells;
}

const isDelimiterRow = (line: string): boolean =>
  line.includes("|") && /^[\s|:-]+$/.test(line) && splitRow(line).every((cell) => /^:?-+:?$/.test(cell));

function alignOf(cell: string): TableAlign {
  const left = cell.startsWith(":");
  const right = cell.endsWith(":");
  return left && right ? "center" : right ? "right" : left ? "left" : null;
}

const FENCE = /^\s*```/;

export function parseMarkdown(source: string): Block[] {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let paragraph: string[] = [];

  const flushParagraph = () => {
    if (paragraph.length) blocks.push({ type: "paragraph", children: parseInline(paragraph.join("\n")) });
    paragraph = [];
  };

  for (let index = 0; index < lines.length; index++) {
    const line = lines[index] ?? "";

    if (FENCE.test(line)) {
      flushParagraph();
      const lang = (/^\s*```\s*([^\s`]*)/.exec(line)?.[1] ?? "").toLowerCase();
      const code: string[] = [];
      index++;
      while (index < lines.length && !FENCE.test(lines[index] ?? "")) {
        code.push(lines[index] ?? "");
        index++;
      }
      const value = code.join("\n");
      const spec = lang === "chart" ? parseChartSpec(value) : null;
      if (spec) blocks.push({ type: "chart", spec });
      else blocks.push(lang ? { type: "code", value, lang } : { type: "code", value });
      continue;
    }

    if (!line.trim()) {
      flushParagraph();
      continue;
    }

    // Таблица GFM: строка заголовка, строка-разделитель `|---|:---:|---:|` с тем же числом колонок, затем строки.
    const next = lines[index + 1];
    if (line.includes("|") && next !== undefined && isDelimiterRow(next)) {
      const header = splitRow(line);
      const delimiter = splitRow(next);
      if (header.length === delimiter.length) {
        flushParagraph();
        const rows: Inline[][][] = [];
        let cursor = index + 2;
        while (cursor < lines.length) {
          const row = lines[cursor] ?? "";
          if (!row.trim() || !row.includes("|") || FENCE.test(row)) break;
          const cells = splitRow(row).slice(0, header.length);
          while (cells.length < header.length) cells.push("");
          rows.push(cells.map(parseInline));
          cursor++;
        }
        blocks.push({
          type: "table",
          align: delimiter.map(alignOf),
          header: header.map(parseInline),
          rows,
        });
        index = cursor - 1;
        continue;
      }
    }

    const heading = /^#{1,6}\s+(.*)$/.exec(line);
    if (heading) {
      flushParagraph();
      blocks.push({ type: "heading", children: parseInline(heading[1] ?? "") });
      continue;
    }

    const bullet = /^\s*[-*•]\s+(.*)$/.exec(line);
    const numbered = /^\s*\d{1,3}[.)]\s+(.*)$/.exec(line);
    if (bullet || numbered) {
      flushParagraph();
      const ordered = Boolean(numbered);
      const items: Inline[][] = [];
      while (index < lines.length) {
        const current = lines[index] ?? "";
        const match = ordered ? /^\s*\d{1,3}[.)]\s+(.*)$/.exec(current) : /^\s*[-*•]\s+(.*)$/.exec(current);
        if (!match) break;
        items.push(parseInline(match[1] ?? ""));
        index++;
      }
      index--;
      blocks.push({ type: "list", ordered, items });
      continue;
    }

    const quote = /^>\s?(.*)$/.exec(line);
    if (quote) {
      flushParagraph();
      const quoted: string[] = [quote[1] ?? ""];
      while (index + 1 < lines.length && /^>\s?/.test(lines[index + 1] ?? "")) {
        index++;
        quoted.push((lines[index] ?? "").replace(/^>\s?/, ""));
      }
      blocks.push({ type: "quote", children: parseInline(quoted.join("\n")) });
      continue;
    }

    paragraph.push(line);
  }
  flushParagraph();
  return blocks;
}
