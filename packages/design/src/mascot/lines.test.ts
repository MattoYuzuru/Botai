import { describe, expect, it } from "vitest";
import { NBSP } from "../lib/format";
import {
  availableLines,
  FLAG_FIELDS,
  LINES,
  type LineField,
  type LineFields,
  type LineKey,
  MAX_LINE_LENGTH,
  pickLine,
  placeholdersOf,
  renderTemplate,
} from "./lines";
import { MASCOT_SITUATIONS } from "./types";

const keys = Object.keys(LINES) as LineKey[];
const variants = keys.flatMap((key) => LINES[key].map((variant) => ({ key, variant })));

/** Типичные значения полей: проверяем, что реплика укладывается в строку. */
const SAMPLE: Record<LineField, string> = {
  space: "ВШЭ",
  elapsed: `1${NBSP}мин 12${NBSP}с`,
  source: "список курсов",
  title: "Домашнее задание 3",
  time: "09:30",
  room: "ауд. 214",
  left: `5${NBSP}ч`,
  ago: `3${NBSP}часа назад`,
  count: `3${NBSP}работы`,
  updatedAt: "24 октября",
  pairs: `3${NBSP}пары`,
  noPairs: "",
  noDue: "",
  allDone: "",
};

describe("реестр реплик §6", () => {
  it("у каждой ситуации renderer есть запись в реестре", () => {
    for (const situation of MASCOT_SITUATIONS) expect(keys).toContain(situation);
  });

  it("каждый шаблон объявляет все свои поля: подставляемое — в needs, лишнее в needs — только признак", () => {
    for (const { key, variant } of variants) {
      const used = placeholdersOf(variant.text);
      for (const field of used) {
        expect(variant.needs, `${key}: «${variant.text}» не объявил {${field}}`).toContain(field);
      }
      for (const field of variant.needs) {
        if (!used.includes(field)) {
          expect(FLAG_FIELDS, `${key}: «${variant.text}» требует ${field}, но не подставляет его`).toContain(field);
        }
      }
    }
  });

  it("шаблон без данных не содержит чисел: придуманных чисел нет", () => {
    for (const { key, variant } of variants) {
      if (variant.needs.length === 0) expect(variant.text, key).not.toMatch(/\d/);
    }
  });

  it("числа появляются только из подставленных полей", () => {
    for (const { key, variant } of variants) {
      const withoutPlaceholders = variant.text.replace(/\{\w+\}/g, "");
      expect(withoutPlaceholders, `${key}: «${variant.text}»`).not.toMatch(/\d/);
    }
  });

  it("каждая реплика с типичными данными — одна строка до 48 знаков, без восклицаний и эмодзи", () => {
    for (const { key, variant } of variants) {
      const text = renderTemplate(variant.text, SAMPLE);
      expect(text, `${key}: «${variant.text}»`).not.toBeNull();
      expect([...(text ?? "")].length, `${key}: «${text}»`).toBeLessThanOrEqual(MAX_LINE_LENGTH);
      expect(text).not.toMatch(/[!]|\p{Extended_Pictographic}/u);
      expect(text).not.toMatch(/\n/);
      expect(text).not.toMatch(/\{|\}/);
    }
  });

  it("реплики не содержат слов об учётных данных и не говорят о результате заранее", () => {
    for (const { variant } of variants) {
      expect(variant.text).not.toMatch(/парол|токен|cookie|куки|логин/i);
    }
    for (const { variant } of LINES.think.map((item) => ({ variant: item }))) {
      expect(variant.text).not.toMatch(/найд|готов|нашл/i);
    }
  });

  it("пропуски в MVP не используются: у skips нет реплик", () => {
    expect(LINES.skips).toEqual([]);
  });

  it("на насесте Варя молчит, пока её не нажали", () => {
    expect(LINES.perch).toEqual([]);
    expect(pickLine("perch", SAMPLE)).toBeNull();
  });
});

describe("renderTemplate и выбор реплики", () => {
  it("не хватает поля — null, пустое значение тоже считается нехваткой", () => {
    expect(renderTemplate("{title} в {time}", { title: "Алгоритмы" })).toBeNull();
    expect(renderTemplate("{title} в {time}", { title: "Алгоритмы", time: "" })).toBeNull();
    expect(renderTemplate("{title} в {time}", { title: "Алгоритмы", time: "09:30" })).toBe("Алгоритмы в 09:30");
  });

  it("берётся самый содержательный вариант; без данных — вариант без числа", () => {
    const full: LineFields = { space: "ВШЭ", elapsed: "12 с" };
    expect(pickLine("sync", full)).toBe("Обновляю ВШЭ, идёт 12 с");
    expect(pickLine("sync", { space: "ВШЭ" })).toBe("Обновляю ВШЭ");
    expect(pickLine("sync", {})).toBe("Обновляю данные");
  });

  it("слишком длинный вариант отбрасывается, а не обрезается", () => {
    const fields: LineFields = { title: "Практическое задание по курсу «Вычислительные методы»", left: "5 ч" };
    expect(pickLine("deadline", fields)).toBe("Осталось 5 ч");
    expect(availableLines("deadline", fields)).toEqual(["Осталось 5 ч"]);
  });

  it("выбор между равными вариантами детерминирован: зерно → индекс", () => {
    const first = pickLine("connect", {}, 0);
    const second = pickLine("connect", {}, 1);
    expect(first).not.toBe(second);
    expect(pickLine("connect", {}, 2)).toBe(first);
    expect(pickLine("connect", {}, 0)).toBe(first);
    // Отрицательное и дробное зерно не ломают выбор.
    expect(pickLine("connect", {}, -1)).toBe(second);
    expect(pickLine("connect", {}, 1.9)).toBe(second);
  });

  it("нет подходящего варианта — null: Варя молчит", () => {
    expect(pickLine("class", {})).toBeNull();
    expect(pickLine("done", {})).toBeNull();
  });

  it("«Пока всё сдано» требует признака, а не просто ситуации done", () => {
    expect(pickLine("done", { allDone: "" })).toBe("Пока всё сдано");
  });
});
