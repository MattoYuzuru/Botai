import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

/**
 * Текущее долгое задание подключения. Нужно, чтобы свёрнутый `ProgressWall` оставался виден в шапке экранов
 * («Подключаю ВШЭ · Нужен ответ») и чтобы после перезагрузки страницы поток задания подхватился снова.
 * Хранится только в sessionStorage вкладки: идентификаторы, без данных и без секретов.
 */
export interface ActiveJob {
  jobId: string;
  title: string;
  spaceTitle?: string;
  connectionId?: string;
}

interface ActiveJobState {
  job: ActiveJob | null;
  set: (job: ActiveJob) => void;
  clear: () => void;
}

export const useActiveJob = create<ActiveJobState>()(
  persist(
    (set) => ({
      job: null,
      set: (job) => set({ job }),
      clear: () => set({ job: null }),
    }),
    {
      name: "botai.activeJob",
      storage: createJSONStorage(() => ({
        getItem: (key) => {
          try {
            return window.sessionStorage.getItem(key);
          } catch {
            return null;
          }
        },
        setItem: (key, value) => {
          try {
            window.sessionStorage.setItem(key, value);
          } catch {
            /* хранилище недоступно */
          }
        },
        removeItem: (key) => {
          try {
            window.sessionStorage.removeItem(key);
          } catch {
            /* хранилище недоступно */
          }
        },
      })),
    },
  ),
);
