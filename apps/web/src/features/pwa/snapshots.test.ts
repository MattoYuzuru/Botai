import { describe, expect, it, vi } from "vitest";
import { clearSnapshots, keepSnapshotsForUser, SNAPSHOT_CACHE } from "./snapshots";
import { API_CACHE } from "./sw/core";

function memory() {
  const rows = new Map<string, string>();
  return {
    rows,
    getItem: (key: string) => rows.get(key) ?? null,
    setItem: (key: string, value: string) => void rows.set(key, value),
  };
}

describe("офлайн-снимки принадлежат вошедшему", () => {
  it("имя кэша на странице совпадает с именем в service worker", () => {
    expect(SNAPSHOT_CACHE).toBe(API_CACHE);
  });

  it("выход стирает кэш снимков; недоступный Cache API не мешает выйти", async () => {
    const del = vi.fn(async () => true);
    await clearSnapshots({ delete: del });
    expect(del).toHaveBeenCalledWith(API_CACHE);
    await expect(clearSnapshots(undefined)).resolves.toBeUndefined();
    await expect(
      clearSnapshots({
        delete: async () => {
          throw new Error("denied");
        },
      }),
    ).resolves.toBeUndefined();
  });

  it("первый вход запоминает владельца и ничего не стирает; тот же человек — ничего не меняется", async () => {
    const storage = memory();
    const del = vi.fn(async () => true);
    expect(await keepSnapshotsForUser("anna", storage, { delete: del })).toBe(false);
    expect(await keepSnapshotsForUser("anna", storage, { delete: del })).toBe(false);
    expect(del).not.toHaveBeenCalled();
  });

  it("другой человек на том же устройстве: снимки прежнего стираются", async () => {
    const storage = memory();
    const del = vi.fn(async () => true);
    await keepSnapshotsForUser("anna", storage, { delete: del });
    expect(await keepSnapshotsForUser("boris", storage, { delete: del })).toBe(true);
    expect(del).toHaveBeenCalledOnce();
    expect(storage.rows.get("botai.snapshot-owner")).toBe("boris");
  });

  it("недоступное хранилище не роняет приложение", async () => {
    const broken = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    };
    await expect(keepSnapshotsForUser("anna", broken, { delete: async () => true })).resolves.toBe(false);
  });
});
