/**
 * Фича-флаг нового интерфейса «Учёбы» (v4) до приёмки владельцем. Пока флаг выключен, экран работает
 * как раньше: старый `StudyScreen` со списком источников и разделами «Документы и вложения»/«Источники».
 *
 * Включается любым из способов:
 * - env `VITE_STUDY_SOURCES_V4=1` — для сборки/стенда;
 * - адрес `?studyUi=v4` (и `?studyUi=v3` для выключения) — для живой приёмки; выбор запоминается
 *   в `localStorage`, чтобы переживать перезагрузку и переходы между экранами;
 * - `localStorage["botai.study.v4"] = "1"` — ручной способ для разработки.
 *
 * Флаг читается один раз за загрузку экрана: смена настройки применяется после перезагрузки страницы
 * или перехода по ссылке с `?studyUi=`.
 */
const STORAGE_KEY = "botai.study.v4";
const QUERY_KEY = "studyUi";

export type StudyUiMode = "v3" | "v4";

interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export interface StudyUiFlagInput {
  /** Значение env `VITE_STUDY_SOURCES_V4` (по умолчанию — из `import.meta.env`). */
  env?: string;
  /** `window.location.search` (по умолчанию — текущий адрес). */
  search?: string;
  /** Хранилище (по умолчанию — `localStorage`, если доступно). */
  storage?: StorageLike | null;
}

function browserStorage(): StorageLike | null {
  try {
    if (typeof window === "undefined") return null;
    return window.localStorage;
  } catch {
    return null; // приватный режим/запрет хранилища: флаг работает без запоминания
  }
}

function envValue(): string {
  try {
    return String(import.meta.env.VITE_STUDY_SOURCES_V4 ?? "");
  } catch {
    return "";
  }
}

/** Режим «Учёбы»: `v4` — новые источники, `v3` — текущий экран. Безопасный ответ — `v3`. */
export function readStudyUiMode(input: StudyUiFlagInput = {}): StudyUiMode {
  const env = (input.env ?? envValue()).trim().toLowerCase();
  if (env === "1" || env === "v4" || env === "true") return "v4";

  const storage = input.storage === undefined ? browserStorage() : input.storage;
  const search = input.search ?? (typeof window === "undefined" ? "" : window.location.search);
  const requested = new URLSearchParams(search).get(QUERY_KEY);

  if (requested === "v4" || requested === "v3") {
    const mode: StudyUiMode = requested === "v4" ? "v4" : "v3";
    try {
      storage?.setItem(STORAGE_KEY, mode === "v4" ? "1" : "0");
    } catch {
      /* хранилище недоступно: режим всё равно действует до перезагрузки */
    }
    return mode;
  }

  try {
    return storage?.getItem(STORAGE_KEY) === "1" ? "v4" : "v3";
  } catch {
    return "v3";
  }
}

/** Запомнить выбор режима (например, переключателем на экране или из теста). */
export function setStudyUiMode(mode: StudyUiMode, storage: StorageLike | null = browserStorage()): void {
  try {
    storage?.setItem(STORAGE_KEY, mode === "v4" ? "1" : "0");
  } catch {
    /* хранилище недоступно — режим остаётся только на текущий рендер */
  }
}

/** Текущий режим «Учёбы» для рендера. Не подписывается на изменения: флаг меняют перезагрузкой/адресом. */
export function useStudyUiMode(): StudyUiMode {
  return readStudyUiMode();
}
