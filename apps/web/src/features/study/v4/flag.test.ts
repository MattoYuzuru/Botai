import { describe, expect, it } from "vitest";
import { readStudyUiMode, type StudyUiFlagInput, setStudyUiMode } from "./flag";

function memoryStorage(initial: Record<string, string> = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value);
    },
    read: () => new Map(values),
  };
}

const read = (input: StudyUiFlagInput) => readStudyUiMode(input);

describe("флаг v4-«Учёбы»", () => {
  it("по умолчанию выключен: безопасный режим — текущий экран", () => {
    expect(read({ env: "", search: "", storage: memoryStorage() })).toBe("v3");
    expect(read({ env: "", search: "", storage: null })).toBe("v3");
  });

  it("env VITE_STUDY_SOURCES_V4=1 включает v4", () => {
    expect(read({ env: "1", search: "", storage: memoryStorage() })).toBe("v4");
    expect(read({ env: "v4", search: "", storage: memoryStorage() })).toBe("v4");
  });

  it("?studyUi=v4 включает и запоминает выбор, ?studyUi=v3 — выключает", () => {
    const storage = memoryStorage();
    expect(read({ env: "", search: "?studyUi=v4", storage })).toBe("v4");
    expect(storage.read().get("botai.study.v4")).toBe("1");

    expect(read({ env: "", search: "?studyUi=v3", storage })).toBe("v3");
    expect(storage.read().get("botai.study.v4")).toBe("0");
  });

  it("запомненный выбор переживает перезагрузку", () => {
    const storage = memoryStorage({ "botai.study.v4": "1" });
    expect(read({ env: "", search: "", storage })).toBe("v4");
    setStudyUiMode("v3", storage);
    expect(read({ env: "", search: "", storage })).toBe("v3");
  });

  it("неизвестный параметр не включает v4; недоступное хранилище не роняет экран", () => {
    expect(read({ env: "", search: "?studyUi=on", storage: memoryStorage() })).toBe("v3");
    const broken = {
      getItem: () => {
        throw new Error("storage denied");
      },
      setItem: () => {
        throw new Error("storage denied");
      },
    };
    expect(read({ env: "", search: "", storage: broken })).toBe("v3");
    expect(read({ env: "", search: "?studyUi=v4", storage: broken })).toBe("v4");
  });
});
