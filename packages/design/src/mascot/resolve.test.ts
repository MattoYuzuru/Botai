import { afterEach, describe, expect, it, vi } from "vitest";
import { NBSP } from "../lib/format";
import { detectDoneEvent, eventKey, resolveMascot } from "./resolve";
import type { MascotContext, MascotDeadline, MascotPresentation, MascotSnapshot } from "./types";

const MSK = "Europe/Moscow";
const HOUR = 3_600_000;
const MINUTE = 60_000;
/** Вторник, 27 октября 2026, 14:05 по Москве. */
const NOW = Date.UTC(2026, 9, 27, 11, 5);
/** Момент в Москве (UTC+3, без перехода на летнее время). */
const msk = (month: number, day: number, hour: number, minute = 0, year = 2026) =>
  Date.UTC(year, month - 1, day, hour - 3, minute);

const plain = (text: string | undefined | null) => (text ?? "").replaceAll(NBSP, " ");

function deadline(id: string, dueMs: number | null, extra: Partial<MascotDeadline> = {}): MascotDeadline {
  return { id, title: `Работа ${id}`, dueMs, done: false, ...extra };
}

function snapshot(extra: Partial<MascotSnapshot> = {}): MascotSnapshot {
  return {
    spaceId: "hse",
    updatedAt: NOW - 5 * MINUTE,
    stale: false,
    nextClass: null,
    deadlines: [],
    ...extra,
  };
}

function ctx(extra: Partial<MascotContext> = {}): MascotContext {
  return {
    spaceId: "hse",
    spaceTitle: "ВШЭ",
    nowMs: NOW,
    timeZone: MSK,
    screen: "chat",
    availability: "ready",
    activity: "idle",
    work: null,
    snapshot: snapshot(),
    recentEvent: null,
    acknowledgedEventIds: [],
    preferences: { seasonal: true, motion: "system" },
    reducedMotion: false,
    lite: false,
    visible: true,
    keyboardOpen: false,
    ...extra,
  };
}

const withDeadlines = (...items: MascotDeadline[]) => snapshot({ deadlines: items });
const situationOf = (extra: Partial<MascotContext>): MascotPresentation["situation"] =>
  resolveMascot(ctx(extra)).situation;

afterEach(() => vi.restoreAllMocks());

describe("resolveMascot: приоритет §2 сверху вниз", () => {
  const urgent = deadline("u", NOW + 5 * HOUR);
  const overdue = deadline("o", NOW - 2 * HOUR);
  const recent = { id: "e1", spaceId: "hse", kind: "deadline-done" as const, at: NOW - 10_000 };

  it("1. reauth выигрывает у всего остального", () => {
    const result = resolveMascot(
      ctx({
        availability: "reauth",
        activity: "syncing",
        snapshot: withDeadlines(urgent),
        recentEvent: recent,
      }),
    );
    expect(result.situation).toBe("reauth");
    expect(result.intent).toEqual({ kind: "reauth", label: "Войти снова" });
  });

  it("2. offline статичен, действия нет, повтор сам при сети", () => {
    const result = resolveMascot(ctx({ availability: "offline", activity: "syncing" }));
    expect(result.situation).toBe("offline");
    expect(result.motion).toBe("static");
    expect(result.intent).toBeNull();
    expect(result.line).toMatch(/^Нет сети/);
  });

  it("3. repairing — это sync с «Что случилось», ведёт в чат", () => {
    const result = resolveMascot(ctx({ availability: "repairing", activity: "connecting" }));
    expect(result.situation).toBe("sync");
    expect(result.intent).toEqual({ kind: "open-chat", label: "Что случилось" });
  });

  it("4. connecting раньше syncing; ведёт к ходу работы", () => {
    const result = resolveMascot(ctx({ activity: "connecting" }));
    expect(result.situation).toBe("connect");
    expect(result.motion).toBe("animated");
    expect(result.intent).toEqual({ kind: "open-progress", label: "Открыть ход" });
  });

  it("5. syncing раньше thinking", () => {
    const result = resolveMascot(ctx({ activity: "syncing" }));
    expect(result.situation).toBe("sync");
    expect(result.intent).toEqual({ kind: "open-progress", label: "Открыть ход" });
  });

  it("6. thinking: think и «К чату»", () => {
    const result = resolveMascot(ctx({ activity: "thinking" }));
    expect(result.situation).toBe("think");
    expect(result.intent).toEqual({ kind: "open-chat", label: "К чату" });
  });

  it("7. свежее неподтверждённое событие раньше просроченного", () => {
    const result = resolveMascot(ctx({ snapshot: withDeadlines(overdue), recentEvent: recent }));
    expect(result.situation).toBe("done");
    expect(result.reactionEventId).toBe("e1");
    expect(result.intent).toEqual({ kind: "open-deadline", label: "Дальше", deadlineId: "o" });
  });

  it("8. просрочено раньше срочного", () => {
    const result = resolveMascot(ctx({ snapshot: withDeadlines(urgent, overdue) }));
    expect(result.situation).toBe("overdue");
    expect(result.intent).toEqual({ kind: "open-deadline", label: "Открыть работу", deadlineId: "o" });
  });

  it("9. до срока ≤ 24 ч: deadline и «Открыть срочную»", () => {
    const result = resolveMascot(ctx({ snapshot: withDeadlines(urgent) }));
    expect(result.situation).toBe("deadline");
    expect(result.intent).toEqual({ kind: "open-deadline", label: "Открыть срочную", deadlineId: "u" });
  });

  it("10. от 24 до 72 ч: tip", () => {
    const result = resolveMascot(ctx({ snapshot: withDeadlines(deadline("s", NOW + 50 * HOUR)) }));
    expect(result.situation).toBe("tip");
    expect(result.intent?.label).toBe("Открыть работу");
  });

  it("граница суток: ровно 24 ч — ещё deadline, 24 ч и минута — уже tip", () => {
    expect(situationOf({ snapshot: withDeadlines(deadline("a", NOW + 24 * HOUR)) })).toBe("deadline");
    expect(situationOf({ snapshot: withDeadlines(deadline("a", NOW + 24 * HOUR + MINUTE)) })).toBe("tip");
  });

  it("граница 72 ч: ровно 72 ч — tip, дальше — ничего срочного", () => {
    expect(situationOf({ snapshot: withDeadlines(deadline("a", NOW + 72 * HOUR)) })).toBe("tip");
    expect(situationOf({ snapshot: withDeadlines(deadline("a", NOW + 72 * HOUR + MINUTE)) })).toBe("idle");
  });

  it("срок ровно «сейчас» — уже просрочено", () => {
    expect(situationOf({ snapshot: withDeadlines(deadline("a", NOW)) })).toBe("overdue");
  });

  it("11. пара в пределах 60 минут: class и «Открыть расписание»", () => {
    const next = { startMs: NOW + 40 * MINUTE, title: "Алгоритмы", room: "ауд. 214" };
    const result = resolveMascot(ctx({ snapshot: snapshot({ nextClass: next }) }));
    expect(result.situation).toBe("class");
    expect(result.intent).toEqual({ kind: "open-schedule", label: "Открыть расписание" });
    expect(result.line).toBe("Алгоритмы в 14:45, ауд. 214");
  });

  it("пара через 61 минуту — не class; пара, которая уже началась, — тоже", () => {
    const far = { startMs: NOW + 61 * MINUTE, title: "Алгоритмы" };
    const started = { startMs: NOW - MINUTE, title: "Алгоритмы" };
    expect(situationOf({ snapshot: snapshot({ nextClass: far }) })).toBe("idle");
    expect(situationOf({ snapshot: snapshot({ nextClass: started }) })).toBe("idle");
  });

  it("срочная работа важнее пары", () => {
    const next = { startMs: NOW + 40 * MINUTE, title: "Алгоритмы" };
    expect(situationOf({ snapshot: snapshot({ nextClass: next, deadlines: [urgent] }) })).toBe("deadline");
  });

  it("12. empty: connect и «Подключить вуз», статично", () => {
    const result = resolveMascot(ctx({ availability: "empty", snapshot: null }));
    expect(result.situation).toBe("connect");
    expect(result.motion).toBe("static");
    expect(result.intent).toEqual({ kind: "connect", label: "Подключить вуз" });
  });

  it("13. ночь 23:00–05:00 по времени пространства: night и тема pj", () => {
    expect(resolveMascot(ctx({ nowMs: msk(10, 27, 23, 0) }))).toMatchObject({ situation: "night", theme: "pj" });
    expect(resolveMascot(ctx({ nowMs: msk(10, 28, 4, 59) })).situation).toBe("night");
    expect(situationOf({ nowMs: msk(10, 27, 22, 59) })).toBe("idle");
    expect(situationOf({ nowMs: msk(10, 28, 5, 0) })).toBe("idle");
  });

  it("ночь — по поясу пространства, а не по поясу устройства", () => {
    const instant = msk(10, 27, 22, 30); // 22:30 в Москве
    expect(situationOf({ nowMs: instant, timeZone: MSK })).toBe("idle");
    expect(situationOf({ nowMs: instant, timeZone: "Asia/Yekaterinburg" })).toBe("night"); // 00:30 там
  });

  it("срочное остаётся срочным и ночью (строка 9 выше строки 13)", () => {
    const nowMs = msk(10, 27, 23, 30);
    expect(situationOf({ nowMs, snapshot: withDeadlines(deadline("a", nowMs + 3 * HOUR)) })).toBe("deadline");
  });

  it("14. всё сдано и снимок свежий: done-поза без реакции, статично", () => {
    const result = resolveMascot(
      ctx({ snapshot: withDeadlines(deadline("a", NOW - HOUR, { done: true }), deadline("b", null, { done: true })) }),
    );
    expect(result).toMatchObject({ situation: "done", motion: "static", reactionEventId: null });
    expect(result.line).toBe("Пока всё сдано");
  });

  it("15. иначе — idle", () => {
    expect(resolveMascot(ctx())).toMatchObject({ situation: "idle", intent: null, badge: null });
  });
});

describe("resolveMascot: фикстуры §8", () => {
  const urgent5 = withDeadlines(deadline("u", NOW + 5 * HOUR, { title: "Домашнее задание 3" }));

  it("urgent: поза deadline, бейдж «Осталось 5 ч», «Открыть срочную»", () => {
    const result = resolveMascot(ctx({ snapshot: urgent5 }));
    expect(result.situation).toBe("deadline");
    expect(result.badge).toEqual({ tone: "warning", text: expect.any(String) });
    expect(plain(result.badge?.text)).toBe("Осталось 5 ч");
    expect(result.intent?.label).toBe("Открыть срочную");
    expect(plain(result.line)).toBe("Домашнее задание 3: осталось 5 ч");
  });

  it("urgent + reducedMotion: то же статично, та же информация и действие", () => {
    const calm = resolveMascot(ctx({ snapshot: urgent5, reducedMotion: true }));
    const live = resolveMascot(ctx({ snapshot: urgent5 }));
    expect(calm.motion).toBe("static");
    expect(live.motion).toBe("animated");
    expect({ ...calm, motion: live.motion }).toEqual(live);
  });

  it("urgent + reauth: первична просьба войти снова, старый срок помечен устаревшим, сезонного слоя нет", () => {
    const result = resolveMascot(
      ctx({ snapshot: urgent5, availability: "reauth", nowMs: msk(12, 25, 12, 0), spaceTitle: "ВШЭ" }),
    );
    expect(result.situation).toBe("reauth");
    expect(result.intent?.kind).toBe("reauth");
    expect(result.line).toBe("ВШЭ просит войти снова");
    // Срок остаётся виден, но помечен устаревшим; сезонный новогодний образ не накладывается.
    expect(plain(result.badge?.text)).toContain("устарело");
    expect(result.skin).toBeNull();
  });

  it("overdue: «Просрочено», без упрёков и без реакции", () => {
    const result = resolveMascot(ctx({ snapshot: withDeadlines(deadline("o", NOW - 3 * HOUR, { title: "Эссе" })) }));
    expect(result.situation).toBe("overdue");
    expect(result.badge).toEqual({ tone: "danger", text: "Просрочено" });
    expect(result.reactionEventId).toBeNull();
    expect(result.motion).toBe("static");
    expect(plain(result.line)).toBe("Срок «Эссе» прошёл 3 часа назад");
    expect(result.line).not.toMatch(/[!]/);
  });

  it("due-unknown: tip, «Срок не указан», обратного отсчёта нет", () => {
    const result = resolveMascot(ctx({ snapshot: withDeadlines(deadline("n", null, { title: "Чтение" })) }));
    expect(result.situation).toBe("tip");
    expect(result.badge).toEqual({ tone: "neutral", text: "Срок не указан" });
    expect(result.line).toBe("Срок «Чтение» не указан");
    expect(result.line).not.toMatch(/\d/);
  });

  it("нечисловой срок (NaN) разбирается как «не указан»", () => {
    const result = resolveMascot(ctx({ snapshot: withDeadlines(deadline("n", Number.NaN)) }));
    expect(result.situation).toBe("tip");
    expect(result.badge?.text).toBe("Срок не указан");
  });

  it("done-once: одна реакция на событие, после подтверждения и по истечении минуты не повторяется", () => {
    const event = { id: "d1", spaceId: "hse", kind: "deadline-done" as const, at: NOW - 5_000, title: "Эссе" };
    const first = resolveMascot(ctx({ recentEvent: event }));
    expect(first).toMatchObject({ situation: "done", reactionEventId: "d1", motion: "animated" });
    expect(first.line).toBe("Эссе сдано");
    // Подтверждено по идентификатору или по ключу «пространство:событие» — после перезагрузки не повторяется.
    expect(resolveMascot(ctx({ recentEvent: event, acknowledgedEventIds: ["d1"] })).reactionEventId).toBeNull();
    expect(
      resolveMascot(ctx({ recentEvent: event, acknowledgedEventIds: [eventKey(event)] })).reactionEventId,
    ).toBeNull();
    // Старше минуты — уже не событие.
    expect(resolveMascot(ctx({ recentEvent: { ...event, at: NOW - 61_000 } })).reactionEventId).toBeNull();
    // Событие «из будущего» (часы разошлись) реакцией не считается.
    expect(resolveMascot(ctx({ recentEvent: { ...event, at: NOW + 5_000 } })).reactionEventId).toBeNull();
  });

  it("no-data: connect + «Подключить вуз», не выглядит как «всё сдано»", () => {
    const result = resolveMascot(ctx({ availability: "empty", snapshot: null }));
    expect(result.situation).toBe("connect");
    expect(result.situation).not.toBe("done");
    expect(result.line).toMatch(/вуз|Подключим/);
  });

  it("пустой список работ при свежем снимке — тоже не «всё сдано»", () => {
    expect(resolveMascot(ctx({ snapshot: snapshot({ deadlines: [] }) })).situation).toBe("idle");
  });

  it("stale-after-failed-sync: «всё сдано» не показывается, срок помечен устаревшим", () => {
    const allDone = withDeadlines(deadline("a", NOW - HOUR, { done: true }));
    expect(resolveMascot(ctx({ snapshot: { ...allDone, stale: true } })).situation).toBe("idle");
    const staleUrgent = resolveMascot(ctx({ snapshot: { ...urgent5, stale: true } }));
    expect(staleUrgent.situation).toBe("deadline");
    expect(plain(staleUrgent.badge?.text)).toBe("Осталось 5 ч · устарело");
  });

  it("space-switch ВШЭ → ЦУ: прежние реплика, бейдж и реакция убраны сразу, состояние unknown", () => {
    const hseEvent = { id: "d1", spaceId: "hse", kind: "deadline-done" as const, at: NOW - 1_000 };
    expect(resolveMascot(ctx({ snapshot: urgent5 })).badge).not.toBeNull();
    expect(resolveMascot(ctx({ recentEvent: hseEvent })).reactionEventId).toBe("d1");
    // Активно ЦУ, а снимок и событие ещё от ВШЭ.
    const after = resolveMascot(
      ctx({
        spaceId: "cu",
        spaceTitle: "ЦУ",
        availability: "unknown",
        snapshot: urgent5,
        recentEvent: hseEvent,
      }),
    );
    expect(after).toMatchObject({ situation: "idle", badge: null, intent: null, reactionEventId: null });
    expect(after.line).not.toMatch(/Работа|ВШЭ|\d/);
  });

  it("thinking: think, «Ищу в …», без обещания результата", () => {
    const result = resolveMascot(ctx({ activity: "thinking" }));
    expect(result.line).toBe("Ищу в ВШЭ");
    expect(result.line).not.toMatch(/найд|готов|\d/i);
  });

  it("реплики sync и connect берут стадию и время из настоящих данных", () => {
    const sync = resolveMascot(
      ctx({ activity: "syncing", work: { startedAtMs: NOW - 72_000, stage: "Читаю расписание" } }),
    );
    expect(plain(sync.line)).toBe("Обновляю ВШЭ, идёт 1 мин 12 с");
    // Время начала неизвестно или «из будущего» — варианта с числом нет.
    expect(resolveMascot(ctx({ activity: "syncing", work: { startedAtMs: null, stage: null } })).line).toBe(
      "Обновляю ВШЭ",
    );
    expect(resolveMascot(ctx({ activity: "syncing", work: { startedAtMs: NOW + 5_000, stage: null } })).line).toBe(
      "Обновляю ВШЭ",
    );
    const connect = resolveMascot(ctx({ activity: "connecting", work: { startedAtMs: NOW, stage: "список курсов" } }));
    expect(connect.line).toBe("Читаю список курсов");
    expect(resolveMascot(ctx({ activity: "connecting" })).line).toBe("Изучаю твой вуз");
  });

  it("offline: реплика называет время снимка, без снимка — без числа", () => {
    const withSnapshot = resolveMascot(ctx({ availability: "offline" }));
    expect(withSnapshot.line).toBe("Нет сети. Это снимок от 14:00");
    const none = resolveMascot(ctx({ availability: "offline", snapshot: null }));
    expect(none.line).toBe("Нет сети");
    const old = resolveMascot(
      ctx({ availability: "offline", snapshot: snapshot({ updatedAt: msk(10, 24, 9, 0) }) }),
    );
    expect(old.line).toBe("Нет сети. Это снимок от 24 октября");
  });

  it("night: число открытых работ из снимка; без данных — «Уже поздно»", () => {
    const nowMs = msk(10, 27, 23, 40);
    const open = withDeadlines(deadline("a", nowMs + 100 * HOUR), deadline("b", nowMs + 200 * HOUR, { done: true }));
    expect(plain(resolveMascot(ctx({ nowMs, snapshot: open })).line)).toBe("1 работа — на утро?");
    expect(resolveMascot(ctx({ nowMs, snapshot: null })).line).toBe("Уже поздно");
    expect(resolveMascot(ctx({ nowMs, snapshot: snapshot() })).line).toBe("Уже поздно");
  });

  it("приветствие пустого чата берёт пары из снимка или обходится без числа", () => {
    const today = { classCount: 3, firstStartMs: msk(10, 27, 9, 30) };
    expect(plain(resolveMascot(ctx({ snapshot: snapshot({ today }) })).line)).toBe("Сегодня 3 пары, первая в 09:30");
    expect(plain(resolveMascot(ctx({ snapshot: snapshot({ today: { ...today, classCount: 5 } }) })).line)).toBe(
      "Сегодня 5 пар, первая в 09:30",
    );
    expect(resolveMascot(ctx({ snapshot: snapshot({ today: { classCount: 0, firstStartMs: null } }) })).line).toBe(
      "Пар сегодня нет",
    );
    // Нет данных о парах — нет числа.
    expect(resolveMascot(ctx()).line).toBe("Спроси меня про дедлайны");
    expect(resolveMascot(ctx({ snapshot: null })).line).toBe("Спроси меня про дедлайны");
  });
});

describe("resolveMascot: бейдж и выбор работы", () => {
  it("несколько срочных работ — одна выбранная (ближайший срок) и «и ещё 2»", () => {
    const result = resolveMascot(
      ctx({
        snapshot: snapshot({
          deadlines: [
            deadline("far", NOW + 20 * HOUR),
            deadline("near", NOW + 2 * HOUR),
            deadline("mid", NOW + 8 * HOUR),
          ],
        }),
      }),
    );
    expect(result.intent?.deadlineId).toBe("near");
    expect(plain(result.badge?.text)).toBe("Осталось 2 ч · и ещё 2");
  });

  it("при равенстве сроков выбор стабилен по id, порядок списка не важен", () => {
    const a = deadline("a", NOW + 3 * HOUR);
    const b = deadline("b", NOW + 3 * HOUR);
    expect(resolveMascot(ctx({ snapshot: snapshot({ deadlines: [b, a] }) })).intent?.deadlineId).toBe("a");
    expect(resolveMascot(ctx({ snapshot: snapshot({ deadlines: [a, b] }) })).intent?.deadlineId).toBe("a");
  });

  it("сданные работы в расчёт срочности не входят", () => {
    const result = resolveMascot(
      ctx({ snapshot: snapshot({ deadlines: [deadline("d", NOW + HOUR, { done: true }), deadline("o", NOW + 60 * HOUR)] }) }),
    );
    expect(result.situation).toBe("tip");
  });

  it("срочность остаётся бейджем при операции, но ситуацию не меняет", () => {
    const result = resolveMascot(
      ctx({ activity: "syncing", snapshot: snapshot({ deadlines: [deadline("u", NOW + 5 * HOUR)] }) }),
    );
    expect(result.situation).toBe("sync");
    expect(plain(result.badge?.text)).toBe("Осталось 5 ч");
  });

  it("просроченное — бейдж «Просрочено» с тоном danger", () => {
    const result = resolveMascot(
      ctx({ activity: "thinking", snapshot: snapshot({ deadlines: [deadline("o", NOW - HOUR), deadline("p", NOW - 2 * HOUR)] }) }),
    );
    expect(result.badge?.tone).toBe("danger");
    expect(plain(result.badge?.text)).toBe("Просрочено · и ещё 1");
  });
});

describe("resolveMascot: движение, экраны и тишина", () => {
  const urgentCtx = { snapshot: snapshot({ deadlines: [deadline("u", NOW + 5 * HOUR)] }) };

  it.each([
    ["reducedMotion", { reducedMotion: true }],
    ["calm", { preferences: { seasonal: true, motion: "calm" as const } }],
    ["lite", { lite: true }],
    ["visible: false", { visible: false }],
    ["keyboardOpen", { keyboardOpen: true }],
  ])("%s: статичная поза, смысл сохранён", (_name, extra) => {
    const live = resolveMascot(ctx(urgentCtx));
    const quiet = resolveMascot(ctx({ ...urgentCtx, ...extra }));
    expect(live.motion).toBe("animated");
    expect(quiet.motion).toBe("static");
    expect(quiet.situation).toBe(live.situation);
    expect(quiet.badge).toEqual(live.badge);
    expect(quiet.intent).toEqual(live.intent);
  });

  it("над клавиатурой пузырь скрыт: реплики нет, поза и бейдж остаются", () => {
    const result = resolveMascot(ctx({ ...urgentCtx, keyboardOpen: true }));
    expect(result.line).toBeNull();
    expect(result.badge).not.toBeNull();
  });

  it("скрытая вкладка прячет картинку, текст остаётся", () => {
    const result = resolveMascot(ctx({ ...urgentCtx, visible: false }));
    expect(result.visible).toBe(false);
    expect(result.line).not.toBeNull();
    expect(result.intent).not.toBeNull();
  });

  it("в покое Варя показывается только в чате; с делом — на любом экране", () => {
    expect(resolveMascot(ctx({ screen: "chat" })).visible).toBe(true);
    expect(resolveMascot(ctx({ screen: "schedule" })).visible).toBe(false);
    expect(resolveMascot(ctx({ screen: "schedule", ...urgentCtx })).visible).toBe(true);
  });
});

describe("resolveMascot: сезонные образы поверх позы", () => {
  const decemberDeadline = (nowMs: number) => snapshot({ deadlines: [deadline("u", nowMs + 5 * HOUR)] });

  it("new-year + urgent: колпак и срок одновременно, срок не затронут", () => {
    const nowMs = msk(12, 25, 12, 0);
    const result = resolveMascot(ctx({ nowMs, snapshot: decemberDeadline(nowMs) }));
    expect(result.situation).toBe("deadline");
    expect(result.skin).toBe("new-year");
    expect(result.seasonalEventId).toBe("new-year");
    expect(plain(result.badge?.text)).toBe("Осталось 5 ч");
    expect(result.intent?.label).toBe("Открыть срочную");
  });

  it("19 декабря 23:59 → 20 декабря 00:00: образ меняется по поясу пространства", () => {
    // Работа агента: поза think не занимает слот головы, ночного колпака нет.
    const instant = msk(12, 19, 23, 59);
    const thinking = { activity: "thinking" as const };
    expect(resolveMascot(ctx({ ...thinking, nowMs: instant })).skin).toBeNull();
    expect(resolveMascot(ctx({ ...thinking, nowMs: instant + MINUTE })).skin).toBe("new-year");
    // Для пространства на 4 часа восточнее та же минута уже в новом окне.
    expect(resolveMascot(ctx({ ...thinking, nowMs: instant, timeZone: "Asia/Krasnoyarsk" })).skin).toBe("new-year");
  });

  it("10 января 23:59 → 11 января 00:00: колпак уходит, срочность не сбрасывается", () => {
    const before = msk(1, 10, 23, 59, 2027);
    const after = before + MINUTE;
    const a = resolveMascot(ctx({ nowMs: before, snapshot: decemberDeadline(before) }));
    const b = resolveMascot(ctx({ nowMs: after, snapshot: decemberDeadline(after) }));
    expect(a.skin).toBe("new-year");
    expect(b.skin).not.toBe("new-year");
    expect(b.skin).toBe("mug"); // сессия: окно «session-winter» продолжается
    for (const result of [a, b]) {
      expect(result.situation).toBe("deadline");
      expect(plain(result.badge?.text)).toBe("Осталось 5 ч");
    }
  });

  it("seasonal: false — базовая Варя, срочность, реплика и действие сохранены", () => {
    const nowMs = msk(12, 25, 12, 0);
    const on = resolveMascot(ctx({ nowMs, snapshot: decemberDeadline(nowMs) }));
    const off = resolveMascot(
      ctx({ nowMs, snapshot: decemberDeadline(nowMs), preferences: { seasonal: false, motion: "system" } }),
    );
    expect(off.skin).toBeNull();
    expect(off.seasonalEventId).toBeNull();
    expect({ ...off, skin: on.skin, seasonalEventId: on.seasonalEventId }).toEqual(on);
  });

  it("ночью голова занята ночным колпаком, образ в слот головы не накладывается", () => {
    const nowMs = msk(12, 25, 23, 30);
    const result = resolveMascot(ctx({ nowMs, snapshot: snapshot() }));
    expect(result.situation).toBe("night");
    expect(result.theme).toBe("pj");
    expect(result.skin).toBeNull();
  });

  it("при ошибке и операции сезонного слоя нет, при работе агента — есть", () => {
    const nowMs = msk(12, 25, 12, 0);
    expect(resolveMascot(ctx({ nowMs, availability: "offline" })).skin).toBeNull();
    expect(resolveMascot(ctx({ nowMs, availability: "repairing" })).skin).toBeNull();
    expect(resolveMascot(ctx({ nowMs, activity: "thinking" })).skin).toBe("new-year");
  });

  it("День знаний 1 сентября — листок, 2 сентября — уже нет", () => {
    expect(resolveMascot(ctx({ nowMs: msk(9, 1, 9, 0) })).skin).toBe("leaf");
    expect(resolveMascot(ctx({ nowMs: msk(9, 2, 0, 0) })).skin).toBeNull();
  });
});

describe("resolveMascot: чистота", () => {
  it("одинаковый вход даёт одинаковый выход и не трогает часы и случайность", () => {
    const now = vi.spyOn(Date, "now").mockImplementation(() => {
      throw new Error("Date.now() вызван");
    });
    const random = vi.spyOn(Math, "random").mockImplementation(() => {
      throw new Error("Math.random() вызван");
    });
    const input = ctx({
      activity: "syncing",
      work: { startedAtMs: NOW - 5_000, stage: "Читаю курсы" },
      snapshot: snapshot({ deadlines: [deadline("u", NOW + 5 * HOUR)] }),
    });
    const first = resolveMascot(input);
    for (let i = 0; i < 20; i++) expect(resolveMascot(input)).toEqual(first);
    expect(now).not.toHaveBeenCalled();
    expect(random).not.toHaveBeenCalled();
  });

  it("не изменяет контекст", () => {
    const input = ctx({ snapshot: snapshot({ deadlines: [deadline("b", NOW + 2 * HOUR), deadline("a", NOW + HOUR)] }) });
    const copy = structuredClone(input);
    resolveMascot(input);
    expect(input).toEqual(copy);
  });

  it("реплика не заменяет текст интерфейса и укладывается в 48 знаков при длинных названиях", () => {
    const longTitle = "Практическое задание по курсу «Вычислительные методы линейной алгебры»";
    const result = resolveMascot(
      ctx({ snapshot: snapshot({ deadlines: [deadline("u", NOW + 5 * HOUR, { title: longTitle })] }) }),
    );
    expect([...(result.line ?? "")].length).toBeLessThanOrEqual(48);
    // Длинное название не влезло — берётся вариант без названия, число остаётся настоящим.
    expect(plain(result.line)).toBe("Осталось 5 ч");
  });
});

describe("detectDoneEvent: событие «сдано» между снимками", () => {
  const before = snapshot({ deadlines: [deadline("a", NOW + HOUR), deadline("b", NOW + 2 * HOUR, { done: true })] });
  const after = snapshot({
    deadlines: [deadline("a", NOW + HOUR, { done: true, title: "Эссе" }), deadline("b", NOW + 2 * HOUR, { done: true })],
  });

  it("работа была открыта и стала сданной — событие", () => {
    expect(detectDoneEvent(before, after, NOW)).toEqual({
      id: "a",
      spaceId: "hse",
      kind: "deadline-done",
      at: NOW,
      title: "Эссе",
    });
  });

  it("первый снимок с уже сданными работами реакций не создаёт", () => {
    expect(detectDoneEvent(null, after, NOW)).toBeNull();
  });

  it("снимок другого пространства — не событие; те же сданные работы — тоже", () => {
    expect(detectDoneEvent({ ...before, spaceId: "cu" }, after, NOW)).toBeNull();
    expect(detectDoneEvent(after, after, NOW)).toBeNull();
  });
});
