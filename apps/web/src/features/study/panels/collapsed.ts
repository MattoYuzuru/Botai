import { useCallback, useState } from "react";

/**
 * Свёрнутые панели помнит браузер (D-039: на сервере — только видимость). Ключ привязан к пользователю, чтобы на
 * общем устройстве чужой выбор не применялся. Хранилище может быть недоступно: тогда выбор живёт до ухода с экрана.
 */
const storageKey = (userId: string) => `botai.study.panels.collapsed.${userId}`;

function read(userId: string): Record<string, true> {
  try {
    const value: unknown = JSON.parse(window.localStorage.getItem(storageKey(userId)) ?? "{}");
    if (typeof value !== "object" || value === null || Array.isArray(value)) return {};
    return Object.fromEntries(Object.entries(value).filter(([, flag]) => flag === true)) as Record<
      string,
      true
    >;
  } catch {
    return {};
  }
}

export function useCollapsedPanels(userId: string) {
  const [collapsed, setCollapsed] = useState(() => read(userId));
  const set = useCallback(
    (panelId: string, value: boolean) => {
      setCollapsed((current) => {
        const next = { ...current };
        if (value) next[panelId] = true;
        else delete next[panelId];
        try {
          window.localStorage.setItem(storageKey(userId), JSON.stringify(next));
        } catch {
          /* хранилище недоступно */
        }
        return next;
      });
    },
    [userId],
  );
  return { isCollapsed: (panelId: string) => collapsed[panelId] === true, set };
}
