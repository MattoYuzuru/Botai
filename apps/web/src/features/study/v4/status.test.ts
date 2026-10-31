import { describe, expect, it } from "vitest";
import type { SourceStatusV4 } from "@/api/sources-v4";
import {
  dataKindsOf,
  describeSourceKind,
  describeSourceStatus,
  kindsLabel,
  sourceIcon,
  summarizeSources,
} from "./status";

const STATUSES: SourceStatusV4[] = [
  "draft",
  "probing",
  "learning",
  "waiting_user",
  "active",
  "syncing",
  "stale",
  "needs_reauth",
  "needs_link",
  "error",
  "disabled",
  "archived",
];

describe("словарь источника v4", () => {
  it("у каждого состояния есть иконка, слова и цвет: цвет не единственный сигнал", () => {
    for (const status of STATUSES) {
      const view = describeSourceStatus(status);
      expect(view.icon.length).toBeGreaterThan(0);
      expect(view.label.length).toBeGreaterThan(0);
      expect(view.detail.length).toBeGreaterThan(0);
    }
  });

  it("действия человека ждут только вход, ссылка и вопрос; выключенный не обновляется", () => {
    const needsUser = STATUSES.filter((status) => describeSourceStatus(status).needsUser);
    expect(needsUser).toEqual(["waiting_user", "needs_reauth", "needs_link"]);
    expect(describeSourceStatus("disabled").disabled).toBe(true);
    expect(describeSourceStatus("active").disabled).toBe(false);
  });

  it("неизвестное состояние с бэкенда не ломает экран", () => {
    const view = describeSourceStatus("что-то-новое");
    expect(view.label).toBe("Неизвестно");
    expect(view.tone).toBe("neutral");
  });

  it("вид источника различает природу и данные; иконка строки — природа", () => {
    expect(describeSourceKind("portal").label).toBe("Личный кабинет");
    expect(describeSourceKind("schedule").label).toBe("Расписание");
    expect(describeSourceKind("неизвестно").label).toBe("Источник");
    expect(sourceIcon(["grades", "portal"])).toBe(describeSourceKind("portal").icon);
    expect(dataKindsOf(["portal", "schedule", "grades"])).toEqual(["schedule", "grades"]);
    expect(kindsLabel(["portal", "schedule", "grades"])).toBe("Расписание, Оценки");
    expect(kindsLabel(["file", "documents"])).toBe("Документы");
    expect(kindsLabel(["mail"])).toBe("Почта");
  });

  it("сводный статус вуза: внимание раньше ошибки, ошибка раньше обновления", () => {
    expect(summarizeSources([])).toBeNull();
    expect(summarizeSources(["disabled", "archived"])).toBeNull();
    expect(summarizeSources(["active", "syncing"])?.label).toBe("Обновляется");
    expect(summarizeSources(["active", "stale"])?.label).toBe("Данные одного источника устарели");
    expect(summarizeSources(["stale", "error"])?.label).toBe("Есть ошибка источника");
    expect(summarizeSources(["error", "needs_reauth"])?.label).toBe("Источник ждёт вашего действия");
    expect(summarizeSources(["active", "active"])?.label).toBe("Всё обновляется");
  });
});
