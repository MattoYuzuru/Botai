import type { ScheduleEvent } from "@/api/types";

/**
 * Склейка источников одного вуза (FR-054): названия источников, из которых собрана пара, основной первым. Один
 * источник — пара не склеена, и строки источников нет.
 */
export function mergedSources(event: Pick<ScheduleEvent, "sources">): string[] | undefined {
  const sources = sourcesOf(event);
  return sources.length > 1 ? sources.map((source) => source.title) : undefined;
}

/**
 * Источники пары. Сервер присылает их всегда, но снимок из офлайн-кэша мог быть сохранён версией приложения
 * до склейки и поля не иметь: такая пара считается парой одного источника.
 */
export function sourcesOf(event: Pick<ScheduleEvent, "sources">): ScheduleEvent["sources"] {
  return event.sources ?? [];
}

/**
 * Спокойная строка о расхождении. Поле расходится — «Источники расходятся: «Календарь» — ауд. 305» (что у
 * другого источника; решает основной). Похожая пара есть в основном источнике, а в этот час нет — слова сервера
 * как есть: пара не скрыта, но, возможно, перенесена или отменена.
 */
export function disagreementNote(event: Pick<ScheduleEvent, "disagreement">): string | null {
  const { disagreement } = event; // у пары из старого офлайн-снимка поля нет
  if (!disagreement) return null;
  return disagreement.kind === "not_in_primary"
    ? disagreement.detail
    : `Источники расходятся: ${disagreement.detail}`;
}
