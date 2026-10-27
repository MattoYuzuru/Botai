import type { TabItem } from "@botai/design";

/** Пять вкладок §7.1: не больше и не меньше. Всё новое — раздел внутри «Учёбы» (SECTION_REGISTRY). */
export const MAIN_TABS = [
  { id: "today", label: "Сегодня", icon: "today", href: "/" },
  { id: "schedule", label: "Расписание", icon: "schedule", href: "/schedule" },
  { id: "tasks", label: "Задания", icon: "deadline", href: "/tasks" },
  { id: "chat", label: "Чат", icon: "chat", href: "/chat" },
  { id: "study", label: "Учёба", icon: "university", href: "/study" },
] as const satisfies readonly TabItem[];

export type TabId = (typeof MAIN_TABS)[number]["id"];
export type TabPath = (typeof MAIN_TABS)[number]["href"];

/** Какая вкладка активна по адресу: вложенные экраны («/study/grades») остаются под своей вкладкой. */
export function activeTab(pathname: string): TabId | null {
  if (pathname === "/") return "today";
  for (const tab of MAIN_TABS) {
    if (tab.href !== "/" && (pathname === tab.href || pathname.startsWith(`${tab.href}/`))) return tab.id;
  }
  return null;
}
