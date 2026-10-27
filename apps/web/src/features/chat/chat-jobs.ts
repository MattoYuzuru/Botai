import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

/**
 * Какое задание сейчас отвечает в каком чате. Нужно, чтобы после перезагрузки страницы поток ответа
 * подхватился по `Last-Event-ID` (сервер держит буфер события до окончания задания и ещё 10 минут), а вопрос
 * агента не потерялся. Хранится в `sessionStorage` вкладки: только идентификаторы и время, без текстов.
 */
export interface ChatJobRef {
  jobId: string;
  /** Когда задание привязано, мс: устаревшую привязку (ответ давно в истории) не поднимаем. */
  at: number;
}

interface ChatJobsState {
  byThread: Record<string, ChatJobRef>;
  track: (threadId: string, jobId: string, at?: number) => void;
  clear: (threadId: string) => void;
  clearAll: () => void;
}

/** Привязка старше этого не поднимается заново: буфер событий на сервере к тому времени уже истёк. */
export const JOB_REF_TTL_MS = 15 * 60_000;

const storage = createJSONStorage(() => ({
  getItem: (key: string) => {
    try {
      return window.sessionStorage.getItem(key);
    } catch {
      return null;
    }
  },
  setItem: (key: string, value: string) => {
    try {
      window.sessionStorage.setItem(key, value);
    } catch {
      /* хранилище недоступно */
    }
  },
  removeItem: (key: string) => {
    try {
      window.sessionStorage.removeItem(key);
    } catch {
      /* хранилище недоступно */
    }
  },
}));

export const useChatJobs = create<ChatJobsState>()(
  persist(
    (set) => ({
      byThread: {},
      track: (threadId, jobId, at = Date.now()) =>
        set((state) => ({ byThread: { ...state.byThread, [threadId]: { jobId, at } } })),
      clear: (threadId) =>
        set((state) => {
          const { [threadId]: _gone, ...rest } = state.byThread;
          return { byThread: rest };
        }),
      clearAll: () => set({ byThread: {} }),
    }),
    { name: "botai.chat.jobs", storage },
  ),
);
