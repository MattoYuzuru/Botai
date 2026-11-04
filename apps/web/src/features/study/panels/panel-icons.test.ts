import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { ICON_NAMES, isIconName } from "@botai/design";
import { describe, expect, it } from "vitest";
import type { PanelIcon } from "@/api/panels";

/**
 * Иконка панели приходит из данных (`spec.icon`), поэтому каждое имя, которое разрешает бэкенд, обязано быть в реестре
 * дизайна (D-039). Список берётся из сгенерированного OpenAPI (`PanelIcon` — union в `schema.gen.ts`); запись
 * `Record<PanelIcon, true>` не даст типу и тесту разойтись при `make gen`.
 */
const ALL_ICONS: Record<PanelIcon, true> = {
  grade: true,
  course: true,
  person: true,
  user: true,
  university: true,
  schedule: true,
  deadline: true,
  table: true,
  number: true,
  group: true,
  teacher: true,
  today: true,
  news: true,
  document: true,
  chart: true,
  trophy: true,
  star: true,
  idCard: true,
  trend: true,
};

function iconsOfGeneratedSchema(): string[] {
  const source = readFileSync(resolve(__dirname, "../../../api/schema.gen.ts"), "utf8");
  const block = source.slice(source.indexOf("KpiSpec: {"));
  const union = /icon: ((?:"[A-Za-z-]+"(?: \| )?)+);/.exec(block)?.[1] ?? "";
  return [...union.matchAll(/"([A-Za-z-]+)"/g)].map((match) => match[1] as string);
}

describe("иконки панелей", () => {
  it("каждое имя из PanelIcon есть в реестре иконок дизайна", () => {
    for (const name of Object.keys(ALL_ICONS)) {
      expect(ICON_NAMES, `иконка ${name}`).toContain(name);
      expect(isIconName(name)).toBe(true);
    }
  });

  it("список в тесте совпадает со сгенерированным union (не отстал от бэкенда)", () => {
    const generated = iconsOfGeneratedSchema();
    expect(generated.length).toBeGreaterThan(10);
    expect([...generated].sort()).toEqual(Object.keys(ALL_ICONS).sort());
    for (const name of generated) expect(ICON_NAMES, `иконка ${name}`).toContain(name);
  });
});
