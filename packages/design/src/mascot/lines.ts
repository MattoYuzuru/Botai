/**
 * Реестр реплик Вари (docs/design/mascot.md §6). Реплики — данные: по 1–3 варианта на ситуацию, выбор
 * детерминирован (зерно → индекс). Числа и названия подставляются **только** из снимка; вариант, которому
 * не хватило поля, не показывается — берётся вариант без числа. Придуманных чисел, процентов, аудиторий и дат
 * нет: каждый шаблон объявляет нужные поля (`needs`), это проверяет тест.
 *
 * Варя — персонаж и обращается на «ты»; интерфейс остаётся безличным и на «вы» (QUESTIONS Q31). Реплика —
 * одна строка до 48 знаков, без восклицаний и эмодзи, и никогда не несёт учётных данных.
 */
import { DEFAULT_TIME_ZONE, dayNumber, formatDay, formatTime } from "../lib/format";
import type { MascotSituationId } from "./types";

/** Поля, которые можно подставить. `noPairs`, `noDue`, `allDone` — признаки без значения (значение — пустая строка). */
export type LineField =
  | "space"
  | "elapsed"
  | "source"
  | "title"
  | "time"
  | "room"
  | "left"
  | "ago"
  | "count"
  | "updatedAt"
  | "pairs"
  | "noPairs"
  | "noDue"
  | "allDone";

/** Признаки: в тексте шаблона их нет, они лишь разрешают вариант. */
export const FLAG_FIELDS: readonly LineField[] = ["noPairs", "noDue", "allDone"];

export interface LineVariant {
  /** Шаблон с `{поле}`. */
  text: string;
  /** Все поля, которые обязан дать снимок: подставляемые и признаки. */
  needs: readonly LineField[];
}

export type LineKey = MascotSituationId | "connect.work" | "done.event" | "done.connected";
export type LineFields = Partial<Record<LineField, string>>;

/** Одна строка — до 48 знаков. */
export const MAX_LINE_LENGTH = 48;

export const LINES: Readonly<Record<LineKey, readonly LineVariant[]>> = {
  // Приветствие пустого чата.
  idle: [
    { text: "Сегодня {pairs}, первая в {time}", needs: ["pairs", "time"] },
    { text: "Пар сегодня нет", needs: ["noPairs"] },
    { text: "Спроси меня про дедлайны", needs: [] },
  ],
  // На насесте молчит, пока её не нажали: тогда берётся реплика текущей ситуации.
  perch: [],
  connect: [
    { text: "Готова изучить твой вуз", needs: [] },
    { text: "Подключим — и всё будет в одном месте", needs: [] },
  ],
  "connect.work": [
    { text: "Читаю {source}", needs: ["source"] },
    { text: "Изучаю твой вуз", needs: [] },
  ],
  sync: [
    { text: "Обновляю {space}, идёт {elapsed}", needs: ["space", "elapsed"] },
    { text: "Обновляю {space}", needs: ["space"] },
    { text: "Обновляю данные", needs: [] },
  ],
  think: [
    { text: "Ищу в {space}", needs: ["space"] },
    { text: "Ищу в твоих данных", needs: [] },
  ],
  class: [
    { text: "{title} в {time}, {room}", needs: ["title", "time", "room"] },
    { text: "{title} в {time}", needs: ["title", "time"] },
    { text: "Скоро пара в {time}", needs: ["time"] },
  ],
  tip: [
    { text: "До «{title}» — {left}", needs: ["title", "left"] },
    { text: "Срок «{title}» не указан", needs: ["title", "noDue"] },
    { text: "До срока — {left}", needs: ["left"] },
    { text: "Срок не указан", needs: ["noDue"] },
  ],
  deadline: [
    { text: "{title}: осталось {left}", needs: ["title", "left"] },
    { text: "Осталось {left}", needs: ["left"] },
  ],
  overdue: [
    { text: "Срок «{title}» прошёл {ago}", needs: ["title", "ago"] },
    { text: "Срок прошёл {ago}", needs: ["ago"] },
    { text: "Срок прошёл", needs: [] },
  ],
  done: [{ text: "Пока всё сдано", needs: ["allDone"] }],
  "done.event": [
    { text: "{title} сдано", needs: ["title"] },
    { text: "Одной работой меньше", needs: [] },
  ],
  "done.connected": [
    { text: "{space} подключён", needs: ["space"] },
    { text: "Вуз подключён", needs: [] },
  ],
  night: [
    { text: "{count} — на утро?", needs: ["count"] },
    { text: "Уже поздно", needs: [] },
  ],
  offline: [
    { text: "Нет сети. Это снимок от {updatedAt}", needs: ["updatedAt"] },
    { text: "Нет сети", needs: [] },
  ],
  reauth: [
    { text: "{space} просит войти снова", needs: ["space"] },
    { text: "Нужно войти снова", needs: [] },
  ],
  error: [
    { text: "Не получилось обновить {space}", needs: ["space"] },
    { text: "Не получилось обновить", needs: [] },
  ],
  // В MVP не используется: данных о пропусках Botai не собирает.
  skips: [],
  wave: [],
};

const PLACEHOLDER = /\{(\w+)\}/g;

/** Подставляемые поля шаблона. */
export function placeholdersOf(template: string): LineField[] {
  return [...template.matchAll(PLACEHOLDER)].map((match) => match[1] as LineField);
}

/** Подставляет поля; `null`, если какого-то не хватает. */
export function renderTemplate(template: string, fields: LineFields): string | null {
  let missing = false;
  const text = template.replace(PLACEHOLDER, (_whole, name: string) => {
    const value = fields[name as LineField];
    if (value === undefined || value === "") {
      missing = true;
      return "";
    }
    return value;
  });
  return missing ? null : text;
}

interface Candidate {
  text: string;
  specificity: number;
}

function candidates(key: LineKey, fields: LineFields): Candidate[] {
  const result: Candidate[] = [];
  for (const variant of LINES[key]) {
    if (!variant.needs.every((name) => fields[name] !== undefined)) continue;
    const text = renderTemplate(variant.text, fields);
    if (text !== null && [...text].length <= MAX_LINE_LENGTH) {
      result.push({ text, specificity: variant.needs.length });
    }
  }
  return result;
}

/** Все реплики, которые можно показать при этих полях: шаблон без нужных данных и слишком длинные отбрасываются. */
export function availableLines(key: LineKey, fields: LineFields): string[] {
  return candidates(key, fields).map((candidate) => candidate.text);
}

/**
 * Реплика ситуации. Из доступных берутся самые содержательные варианты (больше всего подставленных данных), а
 * между равными выбор детерминирован: `seed` (например, `Math.floor(nowMs / 5 мин)` или число нажатий на Варю)
 * → индекс. Так вариант без числа появляется, только когда данных для числа нет. Нет подходящего варианта —
 * `null`, Варя молчит.
 */
export function pickLine(key: LineKey, fields: LineFields, seed = 0): string | null {
  const all = candidates(key, fields);
  if (all.length === 0) return null;
  const best = Math.max(...all.map((candidate) => candidate.specificity));
  const top = all.filter((candidate) => candidate.specificity === best);
  const index = ((Math.trunc(seed) % top.length) + top.length) % top.length;
  return top[index]?.text ?? null;
}

/**
 * Следующая реплика той же ситуации по нажатию на Варю (mascot.md §4.3): все подходящие варианты по кругу, а не
 * только самые содержательные. `current` — реплика, которая сейчас на экране; `step` ≥ 1 — сколько раз нажали.
 */
export function cycleLines(
  key: LineKey,
  fields: LineFields,
  current: string | null,
  step = 1,
): string | null {
  const all = availableLines(key, fields);
  if (all.length === 0) return null;
  const at = current === null ? -1 : all.indexOf(current);
  return all[(((at + step) % all.length) + all.length) % all.length] ?? null;
}

/** «14:05» для сегодняшнего снимка, «24 октября» для более раннего: подпись реплики офлайна. */
export function formatSnapshotTime(ms: number, nowMs: number, timeZone = DEFAULT_TIME_ZONE): string {
  return dayNumber(ms, timeZone) === dayNumber(nowMs, timeZone)
    ? formatTime(ms, timeZone)
    : formatDay(ms, nowMs, timeZone);
}
