/**
 * Подсказка «Добавить на экран „Домой“» для iOS. У iOS нет `beforeinstallprompt`: установить приложение можно
 * только через «Поделиться» в Safari, поэтому подсказку показываем сами — один раз и только там, где она нужна:
 * на iPhone и iPad, пока приложение открыто во вкладке браузера, а не установлено.
 */
export interface InstallEnv {
  userAgent: string;
  platform: string;
  maxTouchPoints: number;
  /** Приложение уже запущено с экрана «Домой» (`navigator.standalone` или `display-mode: standalone`). */
  standalone: boolean;
}

const KEY = "botai.install-hint";

/** iPhone, iPod и iPad, включая iPadOS, который в Safari называет себя Mac (но имеет сенсорный экран). */
export function isIos(env: Pick<InstallEnv, "userAgent" | "platform" | "maxTouchPoints">): boolean {
  if (/iPhone|iPad|iPod/.test(env.userAgent)) return true;
  return env.platform === "MacIntel" && env.maxTouchPoints > 1;
}

/** Показывать ли подсказку: iOS, не установлено и ещё не показывалась. */
export function shouldShowInstallHint(env: InstallEnv, alreadyShown: boolean): boolean {
  return isIos(env) && !env.standalone && !alreadyShown;
}

export function readInstallEnv(): InstallEnv {
  const nav = navigator as Navigator & { standalone?: boolean };
  const displayStandalone = window.matchMedia?.("(display-mode: standalone)").matches ?? false;
  return {
    userAgent: nav.userAgent,
    platform: nav.platform,
    maxTouchPoints: nav.maxTouchPoints,
    standalone: nav.standalone === true || displayStandalone,
  };
}

export function wasInstallHintShown(storage: Pick<Storage, "getItem"> = window.localStorage): boolean {
  try {
    return storage.getItem(KEY) !== null;
  } catch {
    // Хранилище недоступно: лучше не показывать, чем показывать при каждом открытии.
    return true;
  }
}

/** Запоминает, что подсказка показана: больше она не появится. */
export function markInstallHintShown(storage: Pick<Storage, "setItem"> = window.localStorage): void {
  try {
    storage.setItem(KEY, new Date().toISOString());
  } catch {
    /* хранилище недоступно */
  }
}
