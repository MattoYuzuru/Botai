import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { motionPreference } from "./preferences";

/** Подписка на медиа-запрос. На сервере и в окружениях без matchMedia — `fallback`. */
export function useMediaQuery(query: string, fallback = false): boolean {
  return useSyncExternalStore(
    (listener) => {
      const media = window.matchMedia?.(query);
      media?.addEventListener?.("change", listener);
      return () => media?.removeEventListener?.("change", listener);
    },
    () => window.matchMedia?.(query).matches ?? fallback,
    () => fallback,
  );
}

/** Сокращённое движение: системная настройка или «спокойный режим» из настроек приложения. */
export function useCalmMotion(): boolean {
  const system = useMediaQuery("(prefers-reduced-motion: reduce)");
  const preference = motionPreference.use();
  return system || preference === "calm";
}

/** Вкладка видна (document.visibilityState === "visible"). Скрытая вкладка останавливает декоративные циклы. */
export function useDocumentVisible(): boolean {
  return useSyncExternalStore(
    (listener) => {
      document.addEventListener("visibilitychange", listener);
      return () => document.removeEventListener("visibilitychange", listener);
    },
    () => document.visibilityState === "visible",
    () => true,
  );
}

/** Онлайн ли браузер. Только подсказка: настоящую недоступность определяют запросы. */
export function useOnline(): boolean {
  return useSyncExternalStore(
    (listener) => {
      window.addEventListener("online", listener);
      window.addEventListener("offline", listener);
      return () => {
        window.removeEventListener("online", listener);
        window.removeEventListener("offline", listener);
      };
    },
    () => navigator.onLine,
    () => true,
  );
}

/**
 * Часы для интерфейса: возвращает «сейчас» и обновляет его раз в `intervalMs`, пока вкладка видна.
 * `override` подменяет часы целиком — так витрина и тесты получают одинаковый экран при одинаковом времени.
 */
export function useNow(intervalMs = 1000, override?: number): number {
  const visible = useDocumentVisible();
  const [now, setNow] = useState(() => override ?? Date.now());
  useEffect(() => {
    if (override !== undefined || !visible) return;
    setNow(Date.now());
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs, override, visible]);
  return override ?? now;
}

/** Стабильная ссылка на последнее значение — для обработчиков, которые не должны переподписываться. */
export function useLatest<T>(value: T) {
  const ref = useRef(value);
  ref.current = value;
  return ref;
}
