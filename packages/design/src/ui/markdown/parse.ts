/**
 * Безопасный разбор markdown ответа агента. Поддерживается узкое подмножество: абзацы, списки,
 * заголовки, **жирный**, `код`, блоки кода, цитаты и ссылки http(s). HTML не разбирается вовсе —
 * теги остаются обычным текстом; картинки не загружаются (показывается подпись); ссылки-действия,
 * `javascript:`, `data:`, `mailto:` и относительные пути ссылками не становятся (ADR-0004).
 */
import { safeHttpUrl } from "../../lib/safe-url";

export type Inline =
  | { type: "text"; value: string }
  | { type: "bold"; children: Inline[] }
  | { type: "code"; value: string }
  | { type: "link"; href: string; children: Inline[] };

export type Block =
  | { type: "paragraph"; children: Inline[] }
  | { type: "heading"; children: Inline[] }
  | { type: "list"; ordered: boolean; items: Inline[][] }
  | { type: "quote"; children: Inline[] }
  | { type: "code"; value: string };

const URL_RE = /https?:\/\/[^\s<>()[\]"']+[^\s<>()[\]"'.,;:!?]/;

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

    if (rest.startsWith("`")) {
      const end = rest.indexOf("`", 1);
      if (end > 1) {
        flush();
        out.push({ type: "code", value: rest.slice(1, end) });
        i += end + 1;
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
      const url = safeHttpUrl(link[2]);
      flush();
      if (url) out.push({ type: "link", href: url.href, children: parseInline(link[1] ?? "") });
      else out.push({ type: "text", value: link[1] ?? "" });
      i += link[0].length;
      continue;
    }

    const bare = /^https?:\/\//.test(rest) ? URL_RE.exec(rest) : null;
    if (bare && bare.index === 0) {
      const url = safeHttpUrl(bare[0]);
      flush();
      if (url) out.push({ type: "link", href: url.href, children: [{ type: "text", value: bare[0] }] });
      else out.push({ type: "text", value: bare[0] });
      i += bare[0].length;
      continue;
    }

    // Курсива в интерфейсе нет: маркеры *слово* и _слово_ убираем, текст оставляем.
    const emphasis = /^(\*|_)(?=\S)([^*_\n]+?)(?<=\S)\1/.exec(rest);
    if (emphasis && (i === 0 || /\W/.test(source[i - 1] ?? " "))) {
      text += emphasis[2] ?? "";
      i += emphasis[0].length;
      continue;
    }

    text += source[i];
    i++;
  }
  flush();
  return out;
}

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

    if (/^\s*```/.test(line)) {
      flushParagraph();
      const code: string[] = [];
      index++;
      while (index < lines.length && !/^\s*```/.test(lines[index] ?? "")) {
        code.push(lines[index] ?? "");
        index++;
      }
      blocks.push({ type: "code", value: code.join("\n") });
      continue;
    }

    if (!line.trim()) {
      flushParagraph();
      continue;
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
