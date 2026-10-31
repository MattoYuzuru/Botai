import { describe, expect, it } from "vitest";
import { universitySpaces } from "./use-space-directory";

const space = (id: string, universityId: string | null) => ({
  id,
  title: id,
  shortTitle: id,
  aliases: [],
  universityId,
  accentHue: 262,
  timeZone: "Europe/Moscow",
  createdAt: "2026-09-01T00:00:00.000Z",
});

describe("universitySpaces", () => {
  it("оставляет только пространства-вузы: «Мои материалы» не единица интерфейса (D-013)", () => {
    expect(
      universitySpaces([space("hse", "u-hse"), space("materials", null), space("cu", "u-cu")])?.map(
        (row) => row.id,
      ),
    ).toEqual(["hse", "cu"]);
  });

  it("не загруженный ответ остаётся не загруженным, а пустой — пустым", () => {
    expect(universitySpaces(undefined)).toBeUndefined();
    expect(universitySpaces([])).toEqual([]);
  });
});
