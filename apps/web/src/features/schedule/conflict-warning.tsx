import { Banner, IconButton } from "@botai/design";
import { useState } from "react";
import type { ScheduleEvent } from "@/api/types";
import { conflictingCount, conflictSummary } from "./week";

const storageKey = (userId: string) => `botai.schedule.dismissed-conflicts.${userId}`;

/** Источник обновляет fetchedAt при синхронизации; смысл конфликта от этого не меняется. */
export function conflictSignatures(events: readonly ScheduleEvent[]): string[] {
  return events.flatMap((event) =>
    event.conflictsWith.map((partnerId) =>
      JSON.stringify([event.id, Date.parse(event.startsAt), Date.parse(event.endsAt), partnerId]),
    ),
  );
}

function readDismissed(userId: string): string[] {
  try {
    const value: unknown = JSON.parse(window.localStorage.getItem(storageKey(userId)) ?? "[]");
    return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
  } catch {
    return [];
  }
}

/** Закрытие помнит известные пересечения, поэтому их исчезновение не делает остальные «новыми». */
export function ConflictWarning({ userId, events }: { userId: string; events: readonly ScheduleEvent[] }) {
  const [dismissed, setDismissed] = useState(() => readDismissed(userId));
  const signatures = conflictSignatures(events);
  if (!signatures.some((signature) => !dismissed.includes(signature))) return null;

  const dismiss = () => {
    const next = [...new Set([...dismissed, ...signatures])];
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
