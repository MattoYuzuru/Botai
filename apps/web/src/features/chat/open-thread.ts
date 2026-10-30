import { create } from "zustand";

/**
 * Какой тред сейчас открыт на экране чата. Нужен глобальному наблюдателю: уведомление «агент просит войти» нужно
 * только тогда, когда человек смотрит не на этот тред.
 */
export const useOpenThread = create<{ id: string | null; set: (id: string | null) => void }>((set) => ({
  id: null,
  set: (id) => set({ id }),
}));
