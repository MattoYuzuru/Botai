import { create } from "zustand";

interface PwaState {
  /** Новая версия установлена и ждёт: страница показывает «Доступна новая версия». */
  waiting: ServiceWorker | null;
}

export const usePwaStore = create<PwaState>(() => ({ waiting: null }));

/** Просит ждущую версию стать рабочей; страница перезагрузится сама, когда она возьмёт управление. */
export function applyUpdate(): void {
  usePwaStore.getState().waiting?.postMessage({ type: "SKIP_WAITING" });
}

const RECHECK_MS = 30 * 60_000;

/** Минимум нужного для регистрации: контейнер service worker и окно (для подписок). Подменяется в тестах. */
export interface RegisterEnv {
  enabled: boolean;
  container: Pick<ServiceWorkerContainer, "register" | "addEventListener" | "controller"> | undefined;
  window: Pick<Window, "addEventListener" | "location"> & {
    document: Pick<Document, "addEventListener" | "visibilityState">;
  };
  now?: () => number;
}

function defaultEnv(): RegisterEnv {
  return {
    enabled: import.meta.env.PROD,
    container: "serviceWorker" in navigator ? navigator.serviceWorker : undefined,
    window,
  };
}

/**
 * Регистрирует service worker (только в сборке: в dev работает MSW). Обновление не навязывается: новая версия
 * ждёт, пока человек не нажмёт «Перезапустить» в плашке; первая установка перезагрузки не вызывает.
 * Проверка обновлений — при возвращении на вкладку, не чаще раза в полчаса.
 */
export async function registerServiceWorker(env: RegisterEnv = defaultEnv()): Promise<boolean> {
  const { container, enabled } = env;
  if (!enabled || !container) return false;
  const now = env.now ?? Date.now;
  let reloading = false;
  // Управляла ли страницей прежняя версия в момент смены: первая установка берёт страницу под контроль
  // (`clients.claim`) без перезагрузки, а любая следующая смена версии — только после выбора человека.
  let controlled = Boolean(container.controller);

  container.addEventListener("controllerchange", () => {
    const hadController = controlled;
    controlled = Boolean(container.controller);
    if (!hadController || reloading) return;
    reloading = true;
    env.window.location.reload();
  });

  const start = async () => {
    const registration = await container.register("/sw.js", { scope: "/", updateViaCache: "none" });
    const watch = (worker: ServiceWorker | null) => {
      if (!worker) return;
      worker.addEventListener("statechange", () => {
        if (worker.state === "installed" && container.controller) usePwaStore.setState({ waiting: worker });
      });
    };
    if (registration.waiting && container.controller) usePwaStore.setState({ waiting: registration.waiting });
    watch(registration.installing);
    registration.addEventListener("updatefound", () => watch(registration.installing));

    let checkedAt = now();
    env.window.document.addEventListener("visibilitychange", () => {
      if (env.window.document.visibilityState !== "visible" || now() - checkedAt < RECHECK_MS) return;
      checkedAt = now();
      registration.update().catch(() => undefined);
    });
  };

  try {
    await start();
    return true;
  } catch {
    // Регистрация не удалась (например, режим без хранилища): приложение работает без офлайна.
    return false;
  }
}
