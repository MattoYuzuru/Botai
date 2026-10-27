/**
 * Подсказки пустого чата: до трёх вопросов, построенных из настоящих данных человека. Нет данных — нет подсказки:
 * «Какая следующая пара?» не появится, пока в «Сегодня» нет ни одной пары, а «Что у меня просрочено?» — пока нет
 * просроченной открытой работы. Выдуманных названий, чисел и дат здесь нет.
 */
import type { ScheduleEvent } from "@/api/types";

export const MAX_SUGGESTIONS = 3;
const WEEK_MS = 7 * 24 * 3_600_000;
const TITLE_LIMIT = 36;

export interface OpenDeadline {
  title: string;
  /** Срок в мс или `null`, если он не указан. */
  dueMs: number | null;
}

export interface SuggestionInput {
  nowMs: number;
  /** Пары сегодня из всех вузов. */
  events: readonly Pick<ScheduleEvent, "startsAt" | "endsAt">[];
  /** Открытые (не сданные) работы. */
  deadlines: readonly OpenDeadline[];
  /** Названия документов пользователя. */
  documentTitles: readonly string[];
  /** Вузы, источники которых требуют входа или устарели. */
  needsAttention: readonly string[];
}

const short = (title: string) => {
  const clean = title.replace(/\s+/g, " ").trim();
  return clean.length > TITLE_LIMIT ? `${clean.slice(0, TITLE_LIMIT - 1).trimEnd()}…` : clean;
};

export function buildSuggestions(input: SuggestionInput): string[] {
  const result: string[] = [];
  const { nowMs } = input;

  // Пары: «следующая» осмысленна, пока сегодня есть ещё не начавшаяся пара.
  const upcoming = input.events.some((event) => Date.parse(event.startsAt) > nowMs);
  if (upcoming) result.push("Какая следующая пара?");
  else if (input.events.length > 0) result.push("Что сегодня было по расписанию?");

  // Работы: просроченное важнее недели, неделя важнее общего вопроса.
  const open = input.deadlines;
  if (open.some((item) => item.dueMs !== null && item.dueMs <= nowMs)) {
    result.push("Что у меня просрочено?");
  } else if (open.some((item) => item.dueMs !== null && item.dueMs - nowMs <= WEEK_MS)) {
    result.push("Что сдавать на неделе?");
  } else if (open.length > 0) {
    result.push("Какие у меня дедлайны?");
  }

  const document = input.documentTitles.map(short).find(Boolean);
  if (document) result.push(`О чём документ «${document}»?`);

  const space = input.needsAttention[0];
  if (space) result.push(`Почему не обновляется ${space}?`);

  return result.slice(0, MAX_SUGGESTIONS);
}
