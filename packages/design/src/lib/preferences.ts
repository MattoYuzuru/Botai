/**
 * Локальные настройки оформления: тема, спокойное движение, сезонные образы Вари.
 * Хранятся в localStorage (styleguide §2: «выбор хранится локально»), применяются атрибутами
 * на <html>, поэтому работают и до загрузки React (см. inline-скрипт в index.html).
 * Любое обращение к хранилищу обёрнуто в try/catch: в приватном режиме Safari оно бросает.
 */
import { useSyncExternalStore } from "react";

export type ThemePreference = "system" | "light" | "dark";
export type MotionPreference = "system" | "calm";
export type SeasonalPreference = "on" | "off";

export const STORAGE_KEYS = {
  theme: "botai.theme",
  motion: "botai.motion",
  seasonal: "botai.seasonal",
} as const;

/** Цвета строки состояния браузера (meta theme-color) = токен `bg` в обеих темах; сверяется в tokens.test.ts. */
// design-lint-ignore: meta theme-color принимает только литерал, равенство токену bg проверяет tokens.test.ts
export const THEME_COLORS = { light: "#f9fafc", dark: "#0e1115" } as const;

interface Preference<T extends string> {
  read: () => T;
  write: (value: T) => void;
  subscribe: (listener: () => void) => () => void;
  use: () => T;
}

function createPreference<T extends string>(key: string, allowed: readonly T[], fallback: T): Preference<T> {
  const listeners = new Set<() => void>();
  // Значение живёт в памяти, только если хранилище недоступно (приватный режим): до перезагрузки.
  let memory: T | null = null;

  const read = (): T => {
    try {
      const stored = window.localStorage.getItem(key);
      if (stored && (allowed as readonly string[]).includes(stored)) return stored as T;
    } catch {
      /* хранилище недоступно */
    }
    return memory ?? fallback;
  };

  const write = (value: T) => {
    try {
      if (value === fallback) window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, value);
      memory = null;
    } catch {
      memory = value;
    }
    for (const listener of listeners) listener();
  };

  const subscribe = (listener: () => void) => {
    listeners.add(listener);
    const onStorage = (event: StorageEvent) => {
      if (event.key === key || event.key === null) listener();
    };
    window.addEventListener("storage", onStorage);
    return () => {
      listeners.delete(listener);
      window.removeEventListener("storage", onStorage);
    };
  };

  return { read, write, subscribe, use: () => useSyncExternalStore(subscribe, read, () => fallback) };
}

export const themePreference = createPreference<ThemePreference>(
  STORAGE_KEYS.theme,
  ["system", "light", "dark"],
  "system",
);
export const motionPreference = createPreference<MotionPreference>(
  STORAGE_KEYS.motion,
  ["system", "calm"],
  "system",
);
export const seasonalPreference = createPreference<SeasonalPreference>(
  STORAGE_KEYS.seasonal,
  ["on", "off"],
  "on",
);

function systemPrefersDark(): boolean {
  return (
    typeof window !== "undefined" && window.matchMedia?.("(prefers-color-scheme: dark)").matches === true
  );
}

/** Тема, которую видит человек: явный выбор или системная. */
export function effectiveTheme(preference: ThemePreference): "light" | "dark" {
  if (preference === "system") return systemPrefersDark() ? "dark" : "light";
  return preference;
}

/** Ставит data-theme (системная — без атрибута) и цвет строки состояния. */
export function applyTheme(preference: ThemePreference, root: HTMLElement = document.documentElement): void {
  if (preference === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", preference);
  const color = THEME_COLORS[effectiveTheme(preference)];
  const doc = root.ownerDocument;
  let meta = doc.querySelector<HTMLMetaElement>('meta[name="theme-color"]:not([media])');
  for (const extra of doc.querySelectorAll('meta[name="theme-color"][media]')) extra.remove();
  if (!meta) {
    meta = doc.createElement("meta");
    meta.name = "theme-color";
    doc.head.appendChild(meta);
  }
  meta.content = color;
}

export function applyMotion(
  preference: MotionPreference,
  root: HTMLElement = document.documentElement,
): void {
  if (preference === "calm") root.setAttribute("data-motion", "calm");
  else root.removeAttribute("data-motion");
}

/**
 * Применяет сохранённые настройки и следит за их изменением, за системной темой
 * и за видимостью вкладки (скрытая вкладка останавливает декоративные циклы).
 * Вызывается один раз при старте; возвращает функцию отписки.
 */
export function initPreferences(root: HTMLElement = document.documentElement): () => void {
  const sync = () => {
    applyTheme(themePreference.read(), root);
    applyMotion(motionPreference.read(), root);
  };
  const syncVisibility = () => {
    root.setAttribute("data-hidden", String(root.ownerDocument.visibilityState !== "visible"));
  };
  sync();
  syncVisibility();
  const offTheme = themePreference.subscribe(sync);
  const offMotion = motionPreference.subscribe(sync);
  const media = window.matchMedia?.("(prefers-color-scheme: dark)");
  media?.addEventListener?.("change", sync);
  root.ownerDocument.addEventListener("visibilitychange", syncVisibility);
  return () => {
    offTheme();
    offMotion();
    media?.removeEventListener?.("change", sync);
    root.ownerDocument.removeEventListener("visibilitychange", syncVisibility);
  };
}
