import { ALL_SPACES_ID } from "@botai/design";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { deadlinesQuery, scheduleQuery } from "@/features/data/queries";
import { weekOf } from "@/features/schedule/week";

/** Работает ли service worker: без него снимков нет и греть нечего (dev-режим, тесты, браузер без поддержки). */
export function hasServiceWorker(): boolean {
  return (
    typeof navigator !== "undefined" &&
    "serviceWorker" in navigator &&
    Boolean(navigator.serviceWorker.controller)
  );
}

/**
 * «Подогрев» снимков: пока есть сеть, на экране «Сегодня» заодно запрашиваются расписание этой недели и задания
 * — те самые запросы, что откроют вкладки «Расписание» и «Задания». Service worker кладёт ответы в кэш, и без сети
 * эти вкладки показывают последний снимок, даже если человек их ещё не открывал. Греются обе области: «все вузы»
 * и тот вуз, что выбран в переключателе, — у них разные адреса запросов. Это несколько лёгких чтений за сессию.
 */
export function useWarmSnapshots(
  userId: string | undefined,
  timeZone: string | undefined,
  activeSpaceId: string,
): void {
  const queryClient = useQueryClient();
  useEffect(() => {
    // Часовой пояс — у первого пространства: по нему экран «Расписание» считает границы недели.
    if (!userId || !timeZone || !hasServiceWorker() || !navigator.onLine) return;
    const week = weekOf(Date.now(), timeZone);
    const scopes = activeSpaceId === ALL_SPACES_ID ? [undefined] : [undefined, activeSpaceId];
    for (const spaceId of scopes) {
      void queryClient.prefetchQuery(scheduleQuery(spaceId, week.start, week.end));
      void queryClient.prefetchQuery(deadlinesQuery(spaceId, true));
    }
  }, [userId, timeZone, activeSpaceId, queryClient]);
}
