import { Banner, IconButton } from "@botai/design";
import { useState } from "react";
import type { ScheduleEvent } from "@/api/types";
import { conflictingCount, conflictSummary } from "./week";

const DISMISSED_LIMIT = 4_000;
const storageKey = (userId: string) => `botai.schedule.dismissed-conflicts.${userId}`;

/** Источник обновляет fetchedAt при синхронизации; смысл конфликта от этого не меняется. */
export function conflictSignatures(
  events: readonly ScheduleEvent[],
  partnerEvents: readonly ScheduleEvent[] = events,
): string[] | null {
  const byId = new Map(partnerEvents.map((event) => [event.id, event]));
  const signatures = new Set<string>();
  for (const event of events) {
    for (const partnerId of event.conflictsWith) {
      const partner = byId.get(partnerId);
      // Не запоминаем временную сигнатуру без второй стороны: полная неделя ещё может загружаться.
      if (!partner) return null;
      const pair = [event, partner]
        .sort((first, second) => first.id.localeCompare(second.id))
        .map((item) => [item.id, Date.parse(item.startsAt), Date.parse(item.endsAt)]);
      signatures.add(JSON.stringify(pair));
    }
  }
  return [...signatures];
}

function readDismissed(userId: string): string[] {
  try {
    const value: unknown = JSON.parse(window.localStorage.getItem(storageKey(userId)) ?? "[]");
    return Array.isArray(value)
      ? value.filter((item): item is string => typeof item === "string").slice(-DISMISSED_LIMIT)
      : [];
  } catch {
    return [];
  }
}

/** Закрытие помнит известные пересечения, поэтому их исчезновение не делает остальные «новыми». */
export function ConflictWarning({
  userId,
  events,
  partnerEvents = events,
}: {
  userId: string;
  events: readonly ScheduleEvent[];
  partnerEvents?: readonly ScheduleEvent[];
}) {
  const [dismissed, setDismissed] = useState(() => readDismissed(userId));
  const signatures = conflictSignatures(events, partnerEvents);
  const known = new Set(dismissed);
  if (!signatures?.some((signature) => !known.has(signature))) return null;

  const dismiss = () => {
    const next = [...new Set([...readDismissed(userId), ...dismissed, ...signatures])].slice(
      -DISMISSED_LIMIT,
    );
    setDismissed(next);
    try {
      window.localStorage.setItem(storageKey(userId), JSON.stringify(next));
    } catch {
      // При недоступном storage закрытие всё равно действует до ухода с экрана.
    }
  };

  return (
    <Banner tone="warning" icon="warning">
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1">{conflictSummary(conflictingCount(events))}</span>
        <IconButton
          icon="close"
          label="Скрыть предупреждение о пересечениях"
          variant="ghost"
          size="sm"
          className="text-fg-muted"
          onClick={dismiss}
        />
      </div>
    </Banner>
  );
}
