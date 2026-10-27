import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

/**
 * Какие события Варя уже отыграла («сдано», «вуз подключён»). Хранится на устройстве: после перезагрузки страницы и при
 * возврате на вкладку реакция не повторяется (mascot.md §2). Ключ — `пространство:событие` (`eventKey`).
 */
interface AcknowledgedState {
  keys: string[];
  acknowledge: (key: string) => void;
}

/** Старые ключи не нужны: событие моложе минуты, а список — страховка от повторов. */
const LIMIT = 100;

export const useAcknowledged = create<AcknowledgedState>()(
  persist(
    (set) => ({
      keys: [],
      acknowledge: (key) =>
        set((state) => (state.keys.includes(key) ? state : { keys: [...state.keys, key].slice(-LIMIT) })),
    }),
    {
      name: "botai.mascot.acknowledged",
      storage: createJSONStorage(() => ({
        getItem: (name: string) => {
          try {
            return window.localStorage.getItem(name);
          } catch {
            return null;
          }
        },
        setItem: (name: string, value: string) => {
          try {
            window.localStorage.setItem(name, value);
          } catch {
            /* хранилище недоступно: реакция не повторится до перезагрузки */
          }
        },
        removeItem: (name: string) => {
          try {
            window.localStorage.removeItem(name);
          } catch {
            /* хранилище недоступно */
          }
        },
      })),
    },
  ),
);
