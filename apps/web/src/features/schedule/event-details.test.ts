import { describe, expect, it } from "vitest";
import { eventFixture } from "@/test/data-fixtures";
import { attributeIcon, compactFacts, EVENT_FIELDS, eventDetails } from "./event-details";

const MSK = "Europe/Moscow";

const keys = (event: Parameters<typeof eventDetails>[0]) =>
  eventDetails(event, { timeZone: MSK }).facts.map((f) => f.key);

describe("пресет eventDetails: показывает только то, что есть", () => {
  it("у пары без деталей — только время; пустые строки и пробелы полем не считаются", () => {
    const bare = eventFixture({ location: "  ", address: "", teacher: null, group: null, courseTitle: "" });
    expect(keys(bare)).toEqual(["time"]);
    expect(eventDetails(bare, { timeZone: MSK }).facts[0]?.text).toBe("11:30–12:50");
  });

  it("полная пара: поля идут в порядке реестра, каждое со своей иконкой и подписью", () => {
    const event = eventFixture({
      location: "ауд. 305",
      address: "Покровский б-р, д.11",
      teacher: "Петров А. В.",
      group: "ДРИП 3 курс 26/27",
      lessonNumber: 3,
      courseTitle: "Математический анализ",
    });
    const facts = eventDetails(event, { timeZone: MSK }).facts;
    expect(facts.map((fact) => [fact.key, fact.icon, fact.label, fact.text])).toEqual([
      ["time", "time", "Время", "11:30–12:50"],
      ["lesson", "number", "Номер пары", "3-я пара"],
      ["room", "room", "Аудитория", "ауд. 305"],
      ["address", "address", "Адрес", "Покровский б-р, д.11"],
      ["teacher", "teacher", "Преподаватель", "Петров А. В."],
      ["group", "group", "Группа", "ДРИП 3 курс 26/27"],
      ["course", "course", "Предмет", "Математический анализ"],
    ]);
  });

  it("иконки по смыслу: адрес — геометка, аудитория — дверь, здание — учебное здание, время — часы", () => {
    const icon = (key: string) => EVENT_FIELDS.find((field) => field.key === key)?.icon;
    expect(icon("address")).toBe("address");
    expect(icon("room")).toBe("room");
    expect(icon("time")).toBe("time");
    expect(attributeIcon({ label: "Корпус", icon: null })).toBe("building");
    expect(attributeIcon({ label: "Что-то своё", icon: "building" })).toBe("building");
  });

  it("реестр расширяем: новое поле — строка реестра, карточка подхватывает его сама", () => {
    expect(EVENT_FIELDS.map((field) => field.key)).toEqual([
      "time",
      "lesson",
      "room",
      "address",
      "teacher",
      "group",
      "course",
    ]);
  });
});

describe("пресет eventDetails: тип занятия, атрибуты и ссылки", () => {
  it("бейдж — слова источника («Текущий рубежный контроль»), иначе наш вид занятия, у «другого» бейджа нет", () => {
    expect(eventDetails(eventFixture({ kindText: "Текущий рубежный контроль" })).badge).toBe(
      "Текущий рубежный контроль",
    );
    expect(eventDetails(eventFixture({ kind: "seminar" })).badge).toBe("Семинар");
    expect(eventDetails(eventFixture({ kind: "other", kindText: " " })).badge).toBeNull();
  });

  it("атрибуты: иконка — подсказка источника, иначе по подписи, иначе «информация»; повтор значения скрыт", () => {
    const event = eventFixture({
      address: "Покровский б-р, д.11",
      attributes: [
        { label: "Корпус", value: "Главный корпус", icon: "building" },
        { label: "Кампус", value: "Покровка" },
        { label: "Форма контроля", value: "Контрольная работа", icon: null },
        { label: "Адрес здания", value: "покровский б-р, д.11" },
        { label: "Пустое", value: " " },
        { label: "Неизвестная иконка", value: "Что-то", icon: "map-pin" },
      ],
    });
    const attributes = eventDetails(event).facts.filter((fact) => fact.key.startsWith("attribute-"));
    expect(attributes.map((fact) => [fact.label, fact.icon, fact.text])).toEqual([
      ["Корпус", "building", "Главный корпус"],
      ["Кампус", "building", "Покровка"],
      ["Форма контроля", "info", "Контрольная работа"],
      ["Неизвестная иконка", "info", "Что-то"],
    ]);
  });

  it("онлайн-ссылки: подпись источника, иначе хост; небезопасные схемы отбрасываются", () => {
    const event = eventFixture({
      links: [
        { url: "https://my.fakeuni.test/meet/1", label: "Подключиться к занятию" },
        { url: "https://www.fakeuni.test/record", label: "" },
        { url: "javascript:alert(1)", label: "Плохая" },
        { url: "mailto:a@fakeuni.test", label: "Почта" },
      ],
    });
    expect(eventDetails(event).links).toEqual([
      { href: "https://my.fakeuni.test/meet/1", label: "Подключиться к занятию" },
      { href: "https://www.fakeuni.test/record", label: "fakeuni.test" },
    ]);
  });
});

describe("компактный вариант для карточки в списке и сетке", () => {
  it("берёт аудиторию, адрес, преподавателя, группу и учебное здание; время и предмет — только в шите", () => {
    const event = eventFixture({
      location: "ауд. 305",
      address: "Покровский б-р, д.11",
      teacher: "Петров А. В.",
      group: "ДРИП 3 курс 26/27",
      lessonNumber: 3,
      courseTitle: "Математический анализ",
      attributes: [
        { label: "Форма контроля", value: "Контрольная работа", icon: null },
        { label: "Корпус", value: "Главный корпус", icon: "building" },
      ],
    });
    // Здание стоит рядом с адресом и аудиторией, а не в хвосте.
    expect(compactFacts(eventDetails(event)).map((fact) => fact.key)).toEqual([
      "room",
      "address",
      "attribute-1",
      "teacher",
      "group",
    ]);
  });

  it("у пары без деталей компактных строк нет — карточка не рисует пустых заглушек", () => {
    expect(compactFacts(eventDetails(eventFixture()))).toEqual([]);
  });
});
