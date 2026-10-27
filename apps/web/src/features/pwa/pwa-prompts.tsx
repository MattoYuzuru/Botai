import { Banner, Button, toast } from "@botai/design";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouterState } from "@tanstack/react-router";
import { useEffect } from "react";
import { meQuery, spacesQuery } from "@/api/queries";
import { useActiveSpaceStore } from "@/app/active-space";
import { activeTab } from "@/app/nav";
import {
  markInstallHintShown,
  readInstallEnv,
  shouldShowInstallHint,
  wasInstallHintShown,
} from "./install-hint";
import { applyUpdate, usePwaStore } from "./register";
import { keepSnapshotsForUser } from "./snapshots";
import { hasServiceWorker, useWarmSnapshots } from "./warm";

/**
 * Плашки PWA: «Доступна новая версия» (над нижней панелью, слой 70) и, один раз на iPhone и iPad, подсказка
 * «Добавить на экран „Домой“». Заодно следит, чьи снимки лежат в офлайн-кэше: при смене человека они стираются.
 * Ничего из этого не работает в dev-режиме: service worker там не регистрируется.
 */
export function PwaPrompts() {
  const waiting = usePwaStore((state) => state.waiting);
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  // Только чтение уже загруженного: плашки не делают собственных запросов.
  const me = useQuery({ ...meQuery, enabled: false });
  const spaces = useQuery({ ...spacesQuery, enabled: false });
  const userId = me.data?.user.id;
  const inApp = activeTab(pathname) !== null;

  useEffect(() => {
    if (userId) void keepSnapshotsForUser(userId);
  }, [userId]);
  const activeSpace = useActiveSpaceStore((state) => state.id);
  useWarmSnapshots(userId, spaces.data?.[0]?.timeZone, activeSpace);

  // Вернулась сеть: то, что показывал снимок, запрашивается заново сразу, а не ждёт таймера
  // (справочники — `me`, вузы, подключения — сами не помечены как снимок, но тоже могли прийти из кэша).
  const queryClient = useQueryClient();
  useEffect(() => {
    const refresh = () => {
      if (hasServiceWorker()) void queryClient.invalidateQueries();
    };
    window.addEventListener("online", refresh);
    return () => window.removeEventListener("online", refresh);
  }, [queryClient]);

  useEffect(() => {
    if (!userId || !inApp) return;
    if (!shouldShowInstallHint(readInstallEnv(), wasInstallHintShown())) return;
    markInstallHintShown();
    toast("Добавьте BotAI на экран «Домой»", {
      id: "install-hint",
      description:
        "Нажмите «Поделиться» в Safari и выберите «На экран „Домой“»: приложение откроется как обычное и покажет последние данные без сети.",
      duration: Number.POSITIVE_INFINITY,
      action: { label: "Понятно", onClick: () => undefined },
    });
  }, [userId, inApp]);

  if (!waiting) return null;
  return (
    <div
      data-pwa-update=""
      className="pointer-events-none fixed inset-x-0 bottom-safe z-banner mb-16 px-safe-4 pb-2 xs:px-safe-5 lg:pl-rail"
    >
      <div className="pointer-events-auto mx-auto w-full max-w-col shadow-lg">
        <Banner
          tone="info"
          icon="sync"
          live
          action={
            <Button variant="secondary" size="sm" data-touch-expanded="" onClick={applyUpdate}>
              Перезапустить
            </Button>
          }
        >
          Доступна новая версия
        </Banner>
      </div>
    </div>
  );
}
