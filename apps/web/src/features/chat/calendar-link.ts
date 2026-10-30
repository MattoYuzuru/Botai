/**
 * Личная ссылка календаря (Яндекс, Google, Outlook, iCloud…): ссылка экспорта с ключом в адресе открывает весь
 * календарь, поэтому её нельзя отправлять в чат. Клиент узнаёт такую ссылку до очереди отправки: в `localStorage` и
 * на сервер она не попадает. Правила те же, что у сервера (`backend/src/botai/core/calendar_link.py`), а общие
 * примеры, которые обязаны совпасть, лежат в `contracts/calendar-link-vectors.json`: оба теста читают один файл.
 *
 * Ссылка календаря — это экспорт (`webcal://`, файл `.ics`, `/export/ics`, `format=ics`, адреса опубликованных
 * календарей) с ключом (длинная случайная часть, `private-…`, параметр `token` или `key`). Открытый `.ics` без ключа
 * (календарь праздников, страница про подписку) — обычная ссылка.
 */

const ZERO_WIDTH = /\u200b|\u200c|\u200d|\u2060|\ufeff|\u00ad/g;
const NOT_WORD = String.raw`(?<![\p{L}\p{N}_])`;
const LINK = new RegExp(
  String.raw`${NOT_WORD}(?:webcals?|https?)://[^\s<>"'\x60]+|(?<![\p{L}\p{N}_@./:-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}(?::\d+)?/[^\s<>"'\x60]+`,
  "giu",
);
const SPLIT_SCHEME = /(webcals?|https?)\s*:\s*\/\/\s*/giu;
const TRAILING = ".,;:!?)]}»";
const EXPORT_FILE = [".ics", ".ical", ".ifb"];
const EXPORT_QUERY = new Set(["format", "export", "type", "output", "fmt", "download", "feed"]);
const KEY_NAME = /token|key|secret|auth|sig|pass|private|code|sid/i;
const KEY_WORD = /(?:^|[-_.])(?:private|secret|token|key|auth)(?:[-_.]|$)/i;
const KEY_PREFIX = /^(?:private|secret|token|key|auth)[-_.]\S{2,}/i;
const ICLOUD = /^(?:p\d+-)?caldav\.icloud\.com$/i;
const OUTLOOK = /^outlook\.(?:live|office|office365)\.com$|^[\w-]+\.outlook\.com$/i;

/** Сообщение глазами читателя: похожие символы сведены (полноширинное двоеточие), невидимые убраны, `https:// хост` склеен. */
function clean(text: string): string {
  return text
    .normalize("NFKC")
    .replace(ZERO_WIDTH, "")
    .replace(SPLIT_SCHEME, (_, scheme: string) => `${scheme.toLowerCase()}://`);
}

function unquote(value: string): string {
  let result = value;
  for (let round = 0; round < 2; round += 1) {
    try {
      result = decodeURIComponent(result);
    } catch {
      return result;
    }
  }
  return result;
}

interface Parts {
  webcal: boolean;
  host: string;
  path: string;
  query: URLSearchParams;
}

function parts(url: string): Parts | null {
  let raw = url.normalize("NFKC").replace(ZERO_WIDTH, "").trim();
  if (!raw.includes("://")) raw = `https://${raw}`;
  const webcal = /^webcals?:/i.test(raw);
  try {
    const parsed = new URL(webcal ? raw.replace(/^webcals?:/i, "https:") : raw);
    if (!parsed.hostname) return null;
    return { webcal, host: parsed.hostname.toLowerCase(), path: parsed.pathname, query: parsed.searchParams };
  } catch {
    return null;
  }
}

const segments = (path: string): string[] =>
  path
    .split("/")
    .filter(Boolean)
    .map((segment) => unquote(segment));

/** Часть адреса — ключ: длинная неразрывная серия букв и цифр, очень длинная серия чего угодно или слово `private-…`/`token`. */
function keyLike(raw: string): boolean {
  const value = unquote(raw);
  const run = Math.max(0, ...value.split(/[-_.]/).map((part) => part.length));
  const mixed = /\p{L}/u.test(value) && /\d/.test(value);
  return (run >= 16 && mixed) || run >= 32 || KEY_PREFIX.test(value) || KEY_WORD.test(value);
}

function hasKey(p: Parts): boolean {
  if (segments(p.path).some(keyLike)) return true;
  for (const [name, value] of p.query) if ((KEY_NAME.test(name) && value) || keyLike(value)) return true;
  return false;
}

/** Адрес — экспорт календаря (ключ в расчёт не берётся). */
function exportShape(p: Parts): boolean {
  if (p.webcal) return true;
  const parts = segments(p.path).map((segment) => segment.toLowerCase());
  const last = parts.at(-1);
  if (
    last !== undefined &&
    (EXPORT_FILE.some((ext) => last.endsWith(ext)) || ["ics.xml", "export_execute.php"].includes(last))
  )
    return true;
  for (let index = 0; index < parts.length - 1; index += 1) {
    const here = parts[index];
    const after = parts[index + 1] ?? "";
    if (
      here === "ical" ||
      here === "ics" ||
      (here === "export" && (after.startsWith("ics") || after.startsWith("ical")))
    )
      return true;
  }
  for (const [rawName, rawValue] of p.query) {
    const name = rawName.toLowerCase();
    const value = rawValue.toLowerCase();
    if (
      EXPORT_QUERY.has(name) &&
      (["ics", "ical", "icalendar"].includes(value) || (name === "export" && !value))
    )
      return true;
  }
  return false;
}

/** Опубликованные календари сервисов, где каждый адрес — ключ. */
function published(p: Parts, rawPath: string): boolean {
  const path = (() => {
    try {
      return decodeURIComponent(rawPath).toLowerCase();
    } catch {
      return rawPath.toLowerCase();
    }
  })();
  return (
    (ICLOUD.test(p.host) && path.startsWith("/published/")) ||
    (OUTLOOK.test(p.host) && path.includes("/owa/calendar/")) ||
    path.includes("/dav/public-calendars/")
  );
}

/** Один адрес — личная ссылка календаря: экспорт, в котором есть ключ. */
export function isCalendarLink(url: string): boolean {
  const p = parts(url);
  if (p === null) return false;
  return published(p, p.path) || (exportShape(p) && hasKey(p));
}

const stripTrailing = (match: string): string => {
  let end = match.length;
  while (end > 0 && TRAILING.includes(match[end - 1] ?? "")) end -= 1;
  return match.slice(0, end);
};

/** В сообщении есть личная ссылка календаря (с `https://` или без). */
export function hasCalendarLink(text: string): boolean {
  for (const found of clean(text).matchAll(LINK)) if (isCalendarLink(stripTrailing(found[0]))) return true;
  return false;
}

/**
 * Сообщение без личных ссылок календаря: то, что возвращается в поле ввода. Остальные ссылки и слова остаются, лишние
 * пробелы после вырезанной ссылки схлопываются.
 */
export function withoutCalendarLinks(text: string): string {
  return clean(text)
    .replace(LINK, (found) => (isCalendarLink(stripTrailing(found)) ? "" : found))
    .replace(/[ \t]{2,}/g, " ")
    .trim();
}
