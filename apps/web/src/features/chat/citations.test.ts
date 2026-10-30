import { describe, expect, it } from "vitest";
import { type ConnectionRef, groupCitations, type TextSource } from "./citations";
import { internalHref, internalIcon, internalLabel, parseInternalLink } from "./internal-links";

const AT = "2026-10-27T11:00:00.000Z";
const LATER = "2026-10-27T11:30:00.000Z";
const portal: ConnectionRef = {
  id: "C1",
  title: "Единый личный кабинет",
  origin: "https://lk.fakeuni.test",
  spaceTitle: "ВШЭ",
};
const resolve = (url: string) => {
  const link = parseInternalLink(url);
  return link ? { href: internalHref(link), label: internalLabel(link), icon: internalIcon(link) } : null;
};

describe("цитаты по источникам", () => {
  it("три страницы одного сайта — одна запись с названием источника, а не «lk.fakeuni.test» трижды", () => {
    const sources: TextSource[] = [
      { title: "Расписание на сегодня", url: "https://lk.fakeuni.test/timetable", fetchedAt: AT },
      { title: "Расписание на неделю", url: "https://lk.fakeuni.test/timetable/week", fetchedAt: LATER },
      { title: "Расписание на месяц", url: "https://lk.fakeuni.test/timetable/month", fetchedAt: AT },
    ];
    const groups = groupCitations(sources, [portal], resolve);
    expect(groups).toHaveLength(1);
    expect(groups[0]).toMatchObject({
      label: "Единый личный кабинет",
      spaceTitle: "ВШЭ",
      fetchedAt: Date.parse(LATER),
    });
    expect(groups[0]?.items.map((item) => item.title)).toEqual([
      "Расписание на сегодня",
      "Расписание на неделю",
      "Расписание на месяц",
    ]);
  });

  it("ссылка на подключение и страница того же сайта сливаются в один источник", () => {
    const groups = groupCitations(
      [
        { title: "Расписание — личный кабинет", url: "botai://connections/C1", fetchedAt: AT },
        { title: "Расписание на сайте вуза", url: "https://lk.fakeuni.test/timetable", fetchedAt: AT },
      ],
      [portal],
      resolve,
    );
    expect(groups).toHaveLength(1);
    expect(groups[0]?.label).toBe("Единый личный кабинет");
    expect(groups[0]?.items.map((item) => item.internal?.label ?? "внешняя")).toEqual([
      "Открыть подключение в «Учёбе»",
      "внешняя",
    ]);
  });

  it("документы и треды — каждый сам себе источник; одинаковая запись не дублируется", () => {
    const groups = groupCitations(
      [
        { title: "Правила", url: "botai://documents/D1", fetchedAt: AT },
        { title: "Положение", url: "botai://documents/D2", fetchedAt: AT },
        { title: "Правила", url: "botai://documents/D1", fetchedAt: AT },
        { title: "Тот разговор", url: "botai://threads/T1", fetchedAt: AT },
      ],
      [],
      resolve,
    );
    expect(groups.map((group) => group.label)).toEqual(["Правила", "Положение", "Тот разговор"]);
    expect(groups[0]?.items).toHaveLength(1);
  });

  it("сайт без подключения подписан заголовком; разные заголовки — адресом сайта, а не выдуманным именем", () => {
    const single = groupCitations(
      [{ title: "Каталог курсов", url: "https://catalog.example.test/c", fetchedAt: AT }],
      [],
      resolve,
    );
    expect(single[0]?.label).toBe("Каталог курсов");
    const several = groupCitations(
      [
        { title: "Каталог курсов", url: "https://catalog.example.test/c", fetchedAt: AT },
        { title: "Правила приёма", url: "https://catalog.example.test/p", fetchedAt: AT },
      ],
      [],
      resolve,
    );
    expect(several).toHaveLength(1);
    expect(several[0]?.label).toBe("catalog.example.test");
  });

  it("устаревший снимок помечается у группы; небезопасный адрес остаётся текстом без ссылки", () => {
    const groups = groupCitations(
      [
        { title: "Страница", url: "https://lk.fakeuni.test/a", fetchedAt: AT, stale: true },
        { title: "Скрипт", url: "javascript:alert(1)", fetchedAt: AT },
        { title: "Источник", url: "botai://sources/3", fetchedAt: AT },
      ],
      [portal],
      resolve,
    );
    expect(groups[0]?.stale).toBe(true);
    const script = groups.find((group) => group.label === "Скрипт");
    expect(script?.items[0]?.url).toBeNull();
    const numbered = groups.find((group) => group.label === "Источник");
    expect(numbered?.items[0]).toMatchObject({ url: null, internal: undefined });
  });
});
