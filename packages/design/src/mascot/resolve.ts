/**
 * `resolveMascot` — чистая функция состояния Вари (docs/design/mascot.md §2): без запросов, без чтения DOM,
 * хранилищ и stores, без скрытого `Date.now()` и без случайности. Одинаковый вход и `nowMs` дают одинаковый
 * выход — это и есть тест. Пороги срочности (72 ч / 24 ч / срок прошёл) берутся из `lib/urgency`, тех же, что у
 * `DeadlineItem`; своих порогов здесь нет.
 */
import {
  countRu,
  formatAgo,
  formatDuration,
  formatLeftShort,
  formatTime,
  NBSP,
  zonedParts,
} from "../lib/format";
import { type Urgency, urgencyLabel, urgencyOf } from "../lib/urgency";
import { activeSeasonalEvent, type MascotSlot } from "./events";
import { formatSnapshotTime, type LineFields, type LineKey, pickLine } from "./lines";
import type {
  MascotBadge,
  MascotContext,
  MascotDeadline,
  MascotIntent,
  MascotPresentation,
  MascotRecentEvent,
  MascotSituationId,
  MascotSnapshot,
} from "./types";

const MINUTE_MS = 60_000;
/** Событие моложе этого — «свежее»: на него одна реакция. */
export const RECENT_EVENT_MS = 60_000;
/** Пара «скоро», если начинается в пределах часа. */
export const CLASS_SOON_MS = 60 * MINUTE_MS;
/** Реплика меняется не чаще раза в 5 минут: зерно выбора варианта — номер такого окна. */
export const LINE_ROTATION_MS = 5 * MINUTE_MS;
/** Ночь: с 23:00 до 05:00 по времени пространства. */
export const NIGHT_FROM_HOUR = 23;
export const NIGHT_TO_HOUR = 5;

/** Ключ события для подтверждения: событие дедуплицируется по пространству и идентификатору. */
export const eventKey = (event: Pick<MascotRecentEvent, "id" | "spaceId">) => `${event.spaceId}:${event.id}`;

interface Tracked {
  id: string;
  title: string;
  dueMs: number | null;
}

interface UrgencyPick {
  item: Tracked;
  tier: Urgency;
  /** Сколько ещё работ в том же ярусе («и ещё 2»). */
  others: number;
}

interface Decision {
  situation: MascotSituationId;
  intent: MascotIntent | null;
  badge: MascotBadge | null;
  lineKey: LineKey | null;
  fields: LineFields;
  /** Движение по таблице §2 само по себе статично (оффлайн, просрочено, пустое подключение, «всё сдано»). */
  staticByNature: boolean;
  /** Сезонный слой в этой строке допустим (при ошибке и операции его нет). */
  allowSkin: boolean;
  reactionEventId: string | null;
}

/** Слоты на теле, которые занимает поза ситуации: образ в занятый слот не накладывается. */
const OCCUPIED_SLOTS: Partial<Record<MascotSituationId, readonly MascotSlot[]>> = {
  night: ["head", "side"],
};

const finiteOrNull = (value: number | null | undefined): number | null =>
  typeof value === "number" && Number.isFinite(value) ? value : null;

/** Ближайшая к «сейчас» работа яруса; при равенстве — стабильно по `id`. */
function nearest(list: readonly Tracked[], nowMs: number): Tracked | undefined {
  let best: Tracked | undefined;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const item of list) {
    const distance = item.dueMs === null ? Number.POSITIVE_INFINITY : Math.abs(item.dueMs - nowMs);
    if (!best || distance < bestDistance || (distance === bestDistance && item.id < best.id)) {
      best = item;
      bestDistance = distance;
    }
  }
  return best;
}

function isAcknowledged(event: MascotRecentEvent, acknowledged: readonly string[]): boolean {
  return acknowledged.includes(event.id) || acknowledged.includes(eventKey(event));
}

function badgeFor(pick: UrgencyPick, nowMs: number, timeZone: string, stale: boolean): MascotBadge {
  const label = urgencyLabel(pick.tier, pick.item.dueMs, nowMs, timeZone);
  let text = label.badge ?? label.due;
  if (pick.others > 0) text += ` · и${NBSP}ещё${NBSP}${pick.others}`;
  // Старые данные не выдаются за свежие: срок помечен устаревшим.
  if (stale) text += " · устарело";
  const tone = pick.tier === "overdue" ? "danger" : pick.tier === "unknown" ? "neutral" : "warning";
  return { tone, text };
}

/**
 * Какие работы относятся к ярусам срочности. Срок `null` и нечисловой — `unknown`, без отсчёта.
 * Сданные в расчёт не входят.
 */
function groupOpen(snapshot: MascotSnapshot | null, nowMs: number): Record<Urgency, Tracked[]> {
  const tiers: Record<Urgency, Tracked[]> = { overdue: [], urgent: [], soon: [], calm: [], unknown: [] };
  if (!snapshot) return tiers;
  for (const deadline of snapshot.deadlines) {
    if (deadline.done) continue;
    const dueMs = finiteOrNull(deadline.dueMs);
    tiers[urgencyOf(dueMs, nowMs)].push({ id: deadline.id, title: deadline.title, dueMs });
  }
  return tiers;
}

/** Идёт ли ночь по времени пространства (23:00–05:00). */
export function isNight(nowMs: number, timeZone: string): boolean {
  const { hour } = zonedParts(nowMs, timeZone);
  return hour >= NIGHT_FROM_HOUR || hour < NIGHT_TO_HOUR;
}

/** Все работы снимка сданы. Пустой список — не «всё сдано»: отсутствие данных так не выглядит (styleguide §1). */
function allDone(snapshot: MascotSnapshot | null): boolean {
  return Boolean(snapshot && snapshot.deadlines.length > 0 && snapshot.deadlines.every((item) => item.done));
}

export function resolveMascot(ctx: MascotContext): MascotPresentation {
  const now = ctx.nowMs;
  const zone = ctx.timeZone;
  // Снимок и событие чужого пространства отбрасываются целиком: прежние реплика, бейдж и реакция снимаются сразу.
  const snapshot = ctx.snapshot && ctx.snapshot.spaceId === ctx.spaceId ? ctx.snapshot : null;
  const event = ctx.recentEvent && ctx.recentEvent.spaceId === ctx.spaceId ? ctx.recentEvent : null;
  const space = ctx.spaceTitle?.trim() || undefined;
  const stale = snapshot?.stale === true || ctx.availability === "reauth";

  const tiers = groupOpen(snapshot, now);
  const pickTier = (tier: Urgency): UrgencyPick | null => {
    const list = tiers[tier];
    const item = nearest(list, now);
    return item ? { item, tier, others: list.length - 1 } : null;
  };
  const overdue = pickTier("overdue");
  const urgent = pickTier("urgent");
  const soon = pickTier("soon");
  const unknownDue = pickTier("unknown");
  const openCount = Object.values(tiers).reduce((sum, list) => sum + list.length, 0);

  /** Срочность при ошибке или операции остаётся бейджем, но ситуацию не меняет. */
  const operationBadge = (): MascotBadge | null => {
    const pick = overdue ?? urgent ?? soon;
    return pick ? badgeFor(pick, now, zone, stale) : null;
  };
  /** Работа, к которой ведёт «Дальше» после события: самая срочная из открытых. */
  const nextDeadlineId = (): string | undefined => (overdue ?? urgent ?? soon ?? unknownDue)?.item.id;

  const elapsedField = (): string | undefined => {
    const started = finiteOrNull(ctx.work?.startedAtMs);
    return started !== null && started <= now ? formatDuration(now - started) : undefined;
  };
  const fieldsOf = (extra: LineFields): LineFields => {
    const fields: LineFields = {};
    if (space) fields.space = space;
    for (const [key, value] of Object.entries(extra)) {
      if (value !== undefined) fields[key as keyof LineFields] = value;
    }
    return fields;
  };

  const decide = (): Decision => {
    const base = { badge: null, intent: null, staticByNature: false, allowSkin: true, reactionEventId: null };
    // 1. Нужен повторный вход.
    if (ctx.availability === "reauth") {
      return {
        ...base,
        situation: "reauth",
        intent: { kind: "reauth", label: "Войти снова" },
        badge: operationBadge(),
        lineKey: "reauth",
        fields: fieldsOf({}),
        allowSkin: false,
      };
    }
    // 2. Нет сети: снимок остаётся, повтор сам при сети — кнопки нет.
    if (ctx.availability === "offline") {
      return {
        ...base,
        situation: "offline",
        badge: operationBadge(),
        lineKey: "offline",
        fields: fieldsOf(snapshot ? { updatedAt: formatSnapshotTime(snapshot.updatedAt, now, zone) } : {}),
        staticByNature: true,
        allowSkin: false,
      };
    }
    // 3. Вуз что-то поменял, Варя чинит.
    if (ctx.availability === "repairing") {
      return {
        ...base,
        situation: "sync",
        intent: { kind: "open-chat", label: "Что случилось" },
        badge: operationBadge(),
        lineKey: "sync",
        fields: fieldsOf({ elapsed: elapsedField() }),
        allowSkin: false,
      };
    }
    // 4. Подключение вуза.
    if (ctx.activity === "connecting") {
      const stage = ctx.work?.stage?.trim();
      return {
        ...base,
        situation: "connect",
        intent: { kind: "open-progress", label: "Открыть ход" },
        badge: operationBadge(),
        lineKey: "connect.work",
        fields: fieldsOf({ source: stage || undefined }),
      };
    }
    // 5. Синхронизация.
    if (ctx.activity === "syncing") {
      return {
        ...base,
        situation: "sync",
        intent: { kind: "open-progress", label: "Открыть ход" },
        badge: operationBadge(),
        lineKey: "sync",
        fields: fieldsOf({ elapsed: elapsedField() }),
      };
    }
    // 6. Агент думает.
    if (ctx.activity === "thinking") {
      return {
        ...base,
        situation: "think",
        intent: { kind: "open-chat", label: "К чату" },
        badge: operationBadge(),
        lineKey: "think",
        fields: fieldsOf({}),
      };
    }
    // 7. Свежее неподтверждённое событие: одна реакция.
    if (event && !isAcknowledged(event, ctx.acknowledgedEventIds)) {
      const age = now - event.at;
      if (age >= 0 && age < RECENT_EVENT_MS) {
        const next = nextDeadlineId();
        const connected = event.kind === "space-connected";
        return {
          ...base,
          situation: "done",
          intent: next ? { kind: "open-deadline", label: "Дальше", deadlineId: next } : null,
          lineKey: connected ? "done.connected" : "done.event",
          fields: fieldsOf({ title: event.title?.trim() || undefined }),
          reactionEventId: event.id,
        };
      }
    }
    // 8. Есть открытая работа с прошедшим сроком: статичный знак, без упрёков.
    if (overdue) {
      return {
        ...base,
        situation: "overdue",
        intent: { kind: "open-deadline", label: "Открыть работу", deadlineId: overdue.item.id },
        badge: badgeFor(overdue, now, zone, stale),
        lineKey: "overdue",
        fields: fieldsOf({ title: overdue.item.title, ago: formatAgo(overdue.item.dueMs ?? now, now, zone) }),
        staticByNature: true,
      };
    }
    // 9. До срока не больше суток.
    if (urgent) {
      return {
        ...base,
        situation: "deadline",
        intent: { kind: "open-deadline", label: "Открыть срочную", deadlineId: urgent.item.id },
        badge: badgeFor(urgent, now, zone, stale),
        lineKey: "deadline",
        fields: fieldsOf({
          title: urgent.item.title,
          left: formatLeftShort((urgent.item.dueMs ?? now) - now),
        }),
      };
    }
    // 10. От суток до трёх; срок не указан — тот же спокойный совет, но без отсчёта и без выдуманного срока.
    const calmPick = soon ?? unknownDue;
    if (calmPick) {
      const known = calmPick.tier === "soon" && calmPick.item.dueMs !== null;
      return {
        ...base,
        situation: "tip",
        intent: { kind: "open-deadline", label: "Открыть работу", deadlineId: calmPick.item.id },
        badge: badgeFor(calmPick, now, zone, stale),
        lineKey: "tip",
        fields: fieldsOf(
          known
            ? { title: calmPick.item.title, left: formatLeftShort((calmPick.item.dueMs ?? now) - now) }
            : { title: calmPick.item.title, noDue: "" },
        ),
      };
    }
    // 11. Пара начинается в пределах часа.
    const nextClass = snapshot?.nextClass;
    if (nextClass) {
      const left = nextClass.startMs - now;
      if (Number.isFinite(left) && left >= 0 && left <= CLASS_SOON_MS) {
        return {
          ...base,
          situation: "class",
          intent: { kind: "open-schedule", label: "Открыть расписание" },
          lineKey: "class",
          fields: fieldsOf({
            title: nextClass.title,
            time: formatTime(nextClass.startMs, zone),
            room: nextClass.room?.trim() || undefined,
          }),
        };
      }
    }
    // 12. Вуз не подключён: приглашение, а не «всё сдано».
    if (ctx.availability === "empty") {
      return {
        ...base,
        situation: "connect",
        intent: { kind: "connect", label: "Подключить вуз" },
        lineKey: "connect",
        fields: fieldsOf({}),
        staticByNature: true,
      };
    }
    // 13. Ночь по времени пространства.
    if (isNight(now, zone)) {
      return {
        ...base,
        situation: "night",
        lineKey: "night",
        fields: fieldsOf(openCount > 0 ? { count: countRu(openCount, ["работа", "работы", "работ"]) } : {}),
      };
    }
    // 14. Всё сдано и снимок свежий. Пустой список работ — не «всё сдано».
    if (snapshot && !snapshot.stale && allDone(snapshot)) {
      return {
        ...base,
        situation: "done",
        lineKey: "done",
        fields: fieldsOf({ allDone: "" }),
        staticByNature: true,
      };
    }
    // 15. Иначе — в покое.
    const today = snapshot?.today;
    const greeting: LineFields = {};
    if (today) {
      if (today.classCount > 0 && today.firstStartMs !== null && Number.isFinite(today.firstStartMs)) {
        greeting.pairs = countRu(today.classCount, ["пара", "пары", "пар"]);
        greeting.time = formatTime(today.firstStartMs, zone);
      } else if (today.classCount === 0) {
        greeting.noPairs = "";
      }
    }
    return {
      ...base,
      situation: "idle",
      lineKey: "idle",
      fields: fieldsOf(greeting),
    };
  };

  const decision = decide();
  const situation = decision.situation;

  const forcedStatic =
    ctx.reducedMotion ||
    ctx.preferences.motion === "calm" ||
    ctx.lite ||
    !ctx.visible ||
    // Над клавиатурой Варя едет вместе с полем статично, пузырь скрыт (§4.1).
    ctx.keyboardOpen;
  const motion = decision.staticByNature || forcedStatic ? "static" : "animated";

  // Сезонный слой — декор поверх позы; срочность, реплику и действие он не трогает.
  let skin: MascotPresentation["skin"] = null;
  let seasonalEventId: string | null = null;
  if (ctx.preferences.seasonal && decision.allowSkin) {
    const seasonal = activeSeasonalEvent(now, zone, { occupied: OCCUPIED_SLOTS[situation] ?? [] });
    if (seasonal) {
      skin = seasonal.skinId;
      seasonalEventId = seasonal.eventId;
    }
  }

  const line =
    ctx.keyboardOpen || decision.lineKey === null
      ? null
      : pickLine(decision.lineKey, decision.fields, Math.floor(now / LINE_ROTATION_MS));

  return {
    situation,
    theme: situation === "night" ? "pj" : "orig",
    motion,
    line,
    badge: decision.badge,
    intent: decision.intent,
    skin,
    // На чужих экранах Варя нужна, только если ей есть что сказать; в чате она сидит всегда.
    visible: ctx.visible && (ctx.screen === "chat" || situation !== "idle"),
    reactionEventId: decision.reactionEventId,
    seasonalEventId,
  };
}

/**
 * Событие «сдано» между двумя снимками одного пространства: работа была открыта, стала сданной.
 * Первый снимок (`previous === null`) с уже сданными работами реакций не создаёт; снимок другого
 * пространства — тоже. Возвращает первое событие по порядку списка.
 */
export function detectDoneEvent(
  previous: MascotSnapshot | null,
  next: MascotSnapshot,
  nowMs: number,
): MascotRecentEvent | null {
  if (!previous || previous.spaceId !== next.spaceId) return null;
  const wasOpen = new Map<string, MascotDeadline>();
  for (const item of previous.deadlines) if (!item.done) wasOpen.set(item.id, item);
  for (const item of next.deadlines) {
    if (item.done && wasOpen.has(item.id)) {
      return { id: item.id, spaceId: next.spaceId, kind: "deadline-done", at: nowMs, title: item.title };
    }
  }
  return null;
}
