import { describe, expect, it } from "vitest";
import type { ConnectionKind } from "@/api/types";
import { connectionKindIcon, connectionKindTitle, describeTask } from "./connection-status";

describe("вид источника: подпись и иконка", () => {
  it("известные виды названы по-человечески, у каждого своя иконка", () => {
    expect(connectionKindTitle("lms")).toBe("Учебная платформа");
    expect(connectionKindTitle("doc")).toBe("Документация");
    expect(connectionKindIcon("portal")).toBe("university");
    expect(connectionKindIcon("sheet")).toBe("table");
  });

  it("неизвестный вид (устаревший mail или новый от бэкенда) — «Источник» и общая иконка, а не пустота", () => {
    for (const kind of ["mail", "constructor", "toString"] as const) {
      expect(connectionKindTitle(kind as ConnectionKind)).toBe("Источник");
      expect(connectionKindIcon(kind as ConnectionKind)).toBe("connection");
    }
  });
});

describe("describeTask: «Сейчас в работе» из вида и состояния задачи", () => {
  it("название задачи бэкенд не присылает: подпись собирается из kind и state", () => {
    expect(describeTask({ jobId: "j", kind: "sync", state: "running" })).toBe("Обновление данных: идёт");
    expect(describeTask({ jobId: "j", kind: "connect", state: "waiting_user" })).toBe(
      "Подключение: ждёт вашего ответа",
    );
    expect(describeTask({ jobId: "j", kind: "ingest", state: "queued" })).toBe(
      "Чтение документа: ждёт очереди",
    );
    expect(describeTask({ jobId: "j", kind: "repair", state: "running" })).toBe("Починка: идёт");
  });
});
