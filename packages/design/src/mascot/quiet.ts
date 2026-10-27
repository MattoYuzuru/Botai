/**
 * Тихие режимы Вари (docs/design/mascot.md §2 «Движение и остановка», §4.4): reduced motion, «спокойный
 * маскот» из настроек, слабое устройство или сеть (`lite`) и скрытая вкладка. Любой из них даёт статичную позу;
 * смысл (поза, бейдж, реплика, действие) остаётся, исчезает только петля.
 */
import { useSyncExternalStore } from "react";
import { useDocumentVisible, useMediaQuery } from "../lib/hooks";
import { motionPreference, seasonalPreference } from "../lib/preferences";

interface NavigatorHints extends Navigator {
  connection?: {
    saveData?: boolean;
    effectiveType?: string;
    addEventListener?: (type: "change", listener: () => void) => void;
    removeEventListener?: (type: "change", listener: () => void) => void;
  };
  deviceMemory?: number;
}

/** Слабая сеть или устройство: включён режим экономии, сеть 2G, памяти не больше гигабайта. */
export function isLiteEnvironment(nav: NavigatorHints | undefined = globalThis.navigator): boolean {
  if (!nav) return false;
  const connection = nav.connection;
  if (connection?.saveData === true) return true;
  if (connection?.effectiveType === "slow-2g" || connection?.effectiveType === "2g") return true;
  return typeof nav.deviceMemory === "number" && nav.deviceMemory <= 1;
}

function subscribeLite(listener: () => void): () => void {
  const connection = (globalThis.navigator as NavigatorHints | undefined)?.connection;
  connection?.addEventListener?.("change", listener);
  return () => connection?.removeEventListener?.("change", listener);
}

export function useLite(): boolean {
  return useSyncExternalStore(
    subscribeLite,
    () => isLiteEnvironment(),
    () => false,
  );
}

export interface MascotQuietFlags {
  /** Системная настройка `prefers-reduced-motion`. */
  reducedMotion: boolean;
  lite: boolean;
  /** Вкладка видна. */
  visible: boolean;
  preferences: { seasonal: boolean; motion: "system" | "calm" };
}

/**
 * Все тихие режимы одним объектом — ровно то, что `resolveMascot` берёт из `MascotContext`
 * (`reducedMotion`, `lite`, `visible`, `preferences`). Спокойное движение независимо от сезонности.
 */
export function useMascotQuiet(): MascotQuietFlags {
  const reducedMotion = useMediaQuery("(prefers-reduced-motion: reduce)");
  const motion = motionPreference.use();
  const seasonal = seasonalPreference.use();
  const lite = useLite();
  const visible = useDocumentVisible();
  return { reducedMotion, lite, visible, preferences: { seasonal: seasonal === "on", motion } };
}

/** Статична ли Варя при таких флагах (то же правило, что в `resolveMascot`). */
export function isMascotStatic(flags: MascotQuietFlags & { keyboardOpen?: boolean }): boolean {
  return (
    flags.reducedMotion ||
    flags.preferences.motion === "calm" ||
    flags.lite ||
    !flags.visible ||
    flags.keyboardOpen === true
  );
}
