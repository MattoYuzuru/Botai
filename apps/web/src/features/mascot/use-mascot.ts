/**
 * Состояние Вари из данных приложения. Единственное место, где данные учёбы сходятся в `MascotContext`
 * (docs/design/mascot.md §2): renderer получает только готовый объект состояния и ничего больше.
 */
import {
  detectDoneEvent,
  eventKey,
  type MascotActivity,
  type MascotAvailability,
  type MascotContext,
  type MascotDeadline,
  type MascotPresentation,
  type MascotRecentEvent,
  type MascotScreen,
  type MascotSnapshot,
  resolveMascot,
  useMascotQuiet,
  useNow,
  useOnline,
} from "@botai/design";
import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { deadlinesQuery, todayQuery } from "@/features/data/queries";
import {
  type Scope,
  type SpaceDirectory,
  useScope,
  useSpaceDirectory,
} from "@/features/data/use-space-directory";
import { useActiveJob } from "@/features/jobs/active-job";
import { useJob } from "@/features/jobs/job-manager";
import { isTerminal } from "@/features/jobs/job-state";
import { toDeadlineView } from "@/features/tasks/deadlines";
import { useAcknowledged } from "./acknowledged";

export interface MascotFeed {
  screen: MascotScreen;
  /** Работа, о которой экран знает лучше (в чате — «агент думает»). Иначе берётся из подключений и заданий. */
  activity?: MascotActivity;
  /** Идущая работа агента: когда началась и какая стадия, для реплик «Обновляю …». */
  work?: MascotContext["work"];
  keyboardOpen?: boolean;
}

/** Сводное состояние источников выбранной области: приоритет как у `SpaceStatus` (нет сети → вход → починка). */
export function availabilityOf(directory: SpaceDirectory, scope: Scope, online: boolean): MascotAvailability {
  if (!directory.loaded) return "unknown";
  if (directory.none) return "empty";
  const states = scope.spaces.map((space) => space.health.state);
  if (!online || states.includes("offline")) return "offline";
  if (states.includes("reauth")) return "reauth";
  if (states.includes("repairing")) return "repairing";
  // Вузы есть, подключений нет: приглашение подключить, а не «всё сдано».
  if (states.every((state) => state === "none")) return "empty";
  return "ready";
}

/** Состояние Вари: контекст из данных приложения и итог `resolveMascot`. */
export function useMascot(feed: MascotFeed): {
  context: MascotContext;
  presentation: MascotPresentation;
  /** Реакция проиграна: событие подтверждается и не повторится. */
  acknowledge: (eventId: string) => void;
} {
  const directory = useSpaceDirectory();
  const scope = useScope(directory);
  const online = useOnline();
  const quiet = useMascotQuiet();
  const acknowledged = useAcknowledged((state) => state.keys);
  const markAcknowledged = useAcknowledged((state) => state.acknowledge);

  const connectRef = useActiveJob((state) => state.job);
  const connectJob = useJob(connectRef?.jobId) ?? null;
  const connecting = connectJob !== null && !isTerminal(connectJob.state);

  const availability = availabilityOf(directory, scope, online);
  const ready = availability === "ready";
  const syncing = scope.spaces.some((space) => space.health.state === "syncing");
  const activity: MascotActivity =
    feed.activity && feed.activity !== "idle"
      ? feed.activity
      : connecting
        ? "connecting"
        : syncing
          ? "syncing"
          : "idle";
  const nowMs = useNow(activity === "idle" ? 30_000 : 1_000);

  const today = useQuery({ ...todayQuery(), enabled: ready });
  const deadlines = useQuery({ ...deadlinesQuery(scope.spaceId, true), enabled: ready });
  const todayData = today.data;
  const deadlineRows = deadlines.data;

  const snapshot = useMemo<MascotSnapshot | null>(() => {
    if (!todayData || !deadlineRows) return null;
    const inScope = (spaceId: string) => scope.isAll || spaceId === scope.spaceId;
    const events = todayData.data.events
      .filter((event) => inScope(event.provenance.spaceId))
      .map((event) => ({ start: Date.parse(event.startsAt), title: event.title, room: event.location }))
      .filter((event) => Number.isFinite(event.start))
      .sort((a, b) => a.start - b.start);
    const upcoming = events.find((event) => event.start > nowMs);
    const items: MascotDeadline[] = deadlineRows.data.map((row) => {
      const view = toDeadlineView(row, nowMs);
      return { id: row.id, title: row.title, dueMs: view.dueMs, done: view.done };
    });
    const staleRows =
      todayData.data.events.some((event) => event.provenance.stale) ||
      deadlineRows.data.some((row) => row.provenance.stale);
    return {
      spaceId: scope.activeId,
      updatedAt: todayData.snapshotAt ?? today.dataUpdatedAt,
      stale: todayData.snapshotAt !== null || deadlineRows.snapshotAt !== null || staleRows,
      nextClass: upcoming
        ? { startMs: upcoming.start, title: upcoming.title, room: upcoming.room ?? undefined }
        : null,
      today: { classCount: events.length, firstStartMs: events[0]?.start ?? null },
      deadlines: items,
    };
  }, [todayData, deadlineRows, scope.activeId, scope.isAll, scope.spaceId, nowMs, today.dataUpdatedAt]);

  // «Сдано»: работа была открыта и стала сданной между двумя снимками одного пространства. Первый снимок реакций не даёт.
  const [recentEvent, setRecentEvent] = useState<MascotRecentEvent | null>(null);
  const previous = useRef<MascotSnapshot | null>(null);
  useEffect(() => {
    if (!snapshot) {
      previous.current = null;
      return;
    }
    const event = detectDoneEvent(previous.current, snapshot, Date.now());
    previous.current = snapshot;
    if (event) setRecentEvent(event);
  }, [snapshot]);

  const context = useMemo<MascotContext>(
    () => ({
      spaceId: scope.activeId,
      spaceTitle: scope.isAll ? null : (scope.spaces[0]?.shortTitle ?? null),
      nowMs,
      timeZone: scope.timeZone,
      screen: feed.screen,
      availability,
      activity,
      work:
        feed.work ??
        (connecting && connectJob ? { startedAtMs: connectJob.startedAt, stage: connectJob.stage } : null),
      snapshot: availability === "empty" || availability === "unknown" ? null : snapshot,
      recentEvent,
      acknowledgedEventIds: acknowledged,
      preferences: quiet.preferences,
      reducedMotion: quiet.reducedMotion,
      lite: quiet.lite,
      visible: quiet.visible,
      keyboardOpen: feed.keyboardOpen === true,
    }),
    [
      scope,
      nowMs,
      feed.screen,
      feed.work,
      feed.keyboardOpen,
      availability,
      activity,
      connecting,
      connectJob,
      snapshot,
      recentEvent,
      acknowledged,
      quiet,
    ],
  );

  const presentation = useMemo(() => resolveMascot(context), [context]);
  const acknowledge = useCallback(
    (eventId: string) => {
      const event = recentEvent && recentEvent.id === eventId ? recentEvent : null;
      markAcknowledged(event ? eventKey(event) : eventId);
    },
    [recentEvent, markAcknowledged],
  );
  return { context, presentation, acknowledge };
}
