import type { ChartSpec, RankItem } from "@botai/design";
import { DEFAULT_TIME_ZONE, formatDay, isIconName } from "@botai/design";
import type {
  GradePointOut,
  GradesData,
  PanelIcon,
  PanelOut,
  PanelType,
  ProfileData,
  RankingData,
} from "@/api/panels";

/**
 * Чистая часть панелей «Учёбы» (D-039): что показывать, не как. Всё строится только из того, что дал
 * сервер: значения и шкалы — строки источника, числа `valueNum/scaleMax` — для графика, средних и
 * пересчёта между шкалами здесь нет и быть не может (`grades-model.ts`, AGENTS.md п. 10).
 */

export const KNOWN_PANEL_TYPES: readonly PanelType[] = [
  "kpi",
  "chart",
  "table",
  "ranking",
  "profile",
  "list",
];

export const isKnownPanelType = (value: unknown): value is PanelType =>
  typeof value === "string" && (KNOWN_PANEL_TYPES as readonly string[]).includes(value);

/** Иконка из данных — только если она есть в реестре дизайна; иначе панель рисуется без иконки. */
export function panelIcon(icon: PanelIcon | string | undefined) {
  return isIconName(icon) ? icon : undefined;
}

// ---------- Область экрана ----------

export interface PanelScope {
  /** Вузы области в порядке экрана. */
  spaceIds: readonly string[];
  /** Выбран «Все вузы» и вузов больше одного: панели с `hiddenInAll` не показываются, вузы — группами. */
  grouped: boolean;
}

/**
 * Панели области. Порядок — серверный (вуз по возрасту, внутри вуза по `position`); `spaceIds` задаёт
 * порядок групп экрана. В режиме групп панели с `hiddenInAll` скрыты; при одном вузе «Все вузы» —
 * это и есть он, поэтому там скрывать нечего.
 */
export function panelsForScope(panels: readonly PanelOut[], scope: PanelScope): PanelOut[] {
  const order = new Map(scope.spaceIds.map((id, index) => [id, index]));
  return panels
    .filter((panel) => order.has(panel.space.id))
    .filter((panel) => !(scope.grouped && panel.hiddenInAll))
    .map((panel, index) => ({ panel, index }))
    .sort(
      (a, b) =>
        (order.get(a.panel.space.id) ?? 0) - (order.get(b.panel.space.id) ?? 0) ||
        a.panel.position - b.panel.position ||
        a.index - b.index,
    )
    .map(({ panel }) => panel);
}

export interface PanelGroup {
  space: PanelOut["space"];
  panels: PanelOut[];
}

export function groupBySpace(panels: readonly PanelOut[]): PanelGroup[] {
  const groups = new Map<string, PanelGroup>();
  for (const panel of panels) {
    const group = groups.get(panel.space.id) ?? { space: panel.space, panels: [] };
    group.panels.push(panel);
    groups.set(panel.space.id, group);
  }
  return [...groups.values()];
}

// ---------- Разбор панели ----------

export type PanelBody =
  | { type: "kpi"; data: GradesData | null; metrics: readonly ("count" | "latest" | "best")[] }
  | { type: "chart"; data: GradesData | null; variant: "bar" | "line"; x: "date" | "course" }
  | { type: "table"; data: GradesData | null; columns: readonly TableColumnKey[] }
  | { type: "list"; data: GradesData | null }
  | { type: "ranking"; data: RankingData | null; highlightMe: boolean }
  | { type: "profile"; data: ProfileData | null };

export type TableColumnKey = "title" | "course" | "value" | "scale" | "date";

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

function isGradesData(value: unknown): value is GradesData {
  return (
    isObject(value) &&
    value.kind === "grades" &&
    typeof value.total === "number" &&
    Array.isArray(value.points) &&
    value.points.every((point) => isObject(point) && typeof point.value === "string")
  );
}

function isRankingData(value: unknown): value is RankingData {
  return (
    isObject(value) &&
    value.kind === "ranking" &&
    Array.isArray(value.rows) &&
    value.rows.every(
      (row) => isObject(row) && typeof row.label === "string" && typeof row.position === "number",
    )
  );
}

function isProfileData(value: unknown): value is ProfileData {
  return (
    isObject(value) &&
    value.kind === "profile" &&
    Array.isArray(value.fields) &&
    value.fields.every((field) => isObject(field) && typeof field.label === "string")
  );
}

/**
 * Панель в форме, которую умеют рисовать. `null` — тип неизвестен, spec не того типа или данные не той
 * формы: такая панель скрывается, экран не падает (D-039). Данные `null` у включённой панели — не
 * ошибка разбора: `data: null` значит «выключена студентом», и тело здесь не нужно.
 */
export function readPanel(panel: PanelOut): PanelBody | null {
  const type: unknown = panel.type;
  const spec: unknown = panel.spec;
  if (!isKnownPanelType(type) || !isObject(spec) || spec.type !== type) return null;
  const view: Record<string, unknown> = isObject(spec.view) ? spec.view : {};
  const data: unknown = panel.data;
  switch (type) {
    case "kpi": {
      if (data !== null && !isGradesData(data)) return null;
      const metrics = Array.isArray(view.metrics)
        ? view.metrics.filter(
            (m): m is "count" | "latest" | "best" => m === "count" || m === "latest" || m === "best",
          )
        : [];
      return { type, data, metrics: metrics.length > 0 ? metrics : ["count", "latest"] };
    }
    case "chart": {
      if (data !== null && !isGradesData(data)) return null;
      return {
        type,
        data,
        variant: view.variant === "bar" ? "bar" : "line",
        x: view.x === "course" ? "course" : "date",
      };
    }
    case "table": {
      if (data !== null && !isGradesData(data)) return null;
      const columns = Array.isArray(view.columns)
        ? view.columns.filter((c): c is TableColumnKey =>
            ["title", "course", "value", "scale", "date"].includes(c as string),
          )
        : [];
      return { type, data, columns: columns.length > 0 ? columns : ["course", "value", "date"] };
    }
    case "list":
      return data === null || isGradesData(data) ? { type, data } : null;
    case "ranking":
      return data === null || isRankingData(data)
        ? { type, data, highlightMe: view.highlightMe !== false }
        : null;
    case "profile":
      return data === null || isProfileData(data) ? { type, data } : null;
  }
}

// ---------- Оценки как у источника ----------

const numberFormat = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 });
export const formatNumber = (value: number) => numberFormat.format(value);

/** Верх шкалы словами источника: число, если он дал его однозначно; иначе ничего (шкалу не угадываем). */
export const scaleOf = (point: GradePointOut): string | null =>
  point.scaleMax !== null && point.scaleMax !== undefined ? formatNumber(point.scaleMax) : null;

/** «8,5 / 10» — оценка со шкалой источника; без шкалы — просто значение. Текст не пересчитывается. */
export function gradeText(point: GradePointOut): string {
  const scale = scaleOf(point);
  return scale ? `${point.value} / ${scale}` : point.value;
}

/** Подпись шкалы для графика: слова источника («10 баллов»), иначе «до 10». */
export function scaleCaption(point: GradePointOut): string {
  const text = point.scale?.trim();
  if (text) return text;
  const max = scaleOf(point);
  return max ? `до ${max}` : "шкала не указана";
}

export function pointTitle(point: GradePointOut): string {
  return point.title?.trim() || point.course?.trim() || "Оценка";
}

export function dayText(iso: string | null, zone: string = DEFAULT_TIME_ZONE): string | null {
  if (!iso) return null;
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? formatDay(ms, ms, zone) : null;
}

const shortDate = (zone: string) =>
  new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short", timeZone: zone });

// ---------- График ----------

export interface ChartGroup {
  /** Ключ группы: верх шкалы. */
  key: string;
  scaleLabel: string;
  spec: ChartSpec;
}

/**
 * Графики по шкалам: оценки разных шкал не сравниваются и не накладываются, поэтому на каждую шкалу —
 * свой график с подписью шкалы источника. Берутся только оценки с числом и шкалой; порядок — как дал сервер.
 */
export function chartGroups(
  data: GradesData,
  options: { variant: "bar" | "line"; x: "date" | "course"; title: string; zone?: string },
): ChartGroup[] {
  const zone = options.zone ?? DEFAULT_TIME_ZONE;
  const formatDate = shortDate(zone);
  const byScale = new Map<number, GradePointOut[]>();
  for (const point of data.points) {
    if (point.valueNum === null || point.valueNum === undefined) continue;
    if (point.scaleMax === null || point.scaleMax === undefined) continue;
    if (options.x === "date" && !Number.isFinite(Date.parse(point.gradedAt ?? ""))) continue;
    byScale.set(point.scaleMax, [...(byScale.get(point.scaleMax) ?? []), point]);
  }
  return [...byScale.entries()].map(([max, points]) => {
    const first = points[0] as GradePointOut;
    const scaleLabel = scaleCaption(first);
    const labels = points.map((point) => {
      if (options.x === "date") {
        return formatDate.format(Date.parse(point.gradedAt ?? "")).replace(/\.$/, "");
      }
      return point.course?.trim() || pointTitle(point);
    });
    return {
      key: String(max),
      scaleLabel,
      spec: {
        type: options.variant,
        title: `${options.title}, шкала ${scaleLabel}`,
        labels,
        series: [{ name: `Оценка (${scaleLabel})`, values: points.map((point) => point.valueNum as number) }],
      },
    };
  });
}

// ---------- Рейтинг ----------

export function rankItems(data: RankingData, highlightMe: boolean): RankItem[] {
  return data.rows.map((row) => ({
    id: `${row.position}-${row.label}`,
    position: row.position,
    label: row.label,
    score: row.score,
    isMe: highlightMe && row.isMe,
  }));
}

/** Сколько участников всего: сначала число у строки «Вы», затем размер, который назвал источник. */
export function rankTotal(data: RankingData): number | null {
  const me = data.rows.find((row) => row.isMe);
  return me?.total ?? data.participants ?? null;
}

// ---------- Профиль ----------

const NAME_KEYS = new Set(["name", "full_name", "fullname", "fio", "student_name", "studentname"]);
const NAME_LABELS = new Set(["фио", "имя", "студент", "фамилия имя отчество"]);

/** Имя студента, если источник дал его отдельным полем: идёт в шапку карточки, не в список. */
export function profileView(data: ProfileData): {
  name: string | null;
  fields: { label: string; value: string }[];
} {
  const nameField = data.fields.find(
    (field) =>
      field.value.trim() !== "" &&
      (NAME_KEYS.has(field.field.toLowerCase()) || NAME_LABELS.has(field.label.trim().toLowerCase())),
  );
  const seen = new Map<string, number>();
  const fields = data.fields
    .filter((field) => field !== nameField)
    .map((field) => {
      const base = field.label.trim() || field.field;
      const count = seen.get(base) ?? 0;
      seen.set(base, count + 1);
      // Одинаковые подписи в разных группах различаем группой: ключи списка и смысл остаются однозначными.
      const label =
        count > 0 && field.group ? `${base} (${field.group})` : count > 0 ? `${base} ${count + 1}` : base;
      return { label, value: field.value };
    });
  return { name: nameField?.value.trim() ?? null, fields };
}

// ---------- Раскладка ----------

export interface PanelLayout {
  /** Показатели: лента плиток на всю ширину. */
  band: PanelOut[];
  /** Широкие панели (график, таблица, список): основная колонка. */
  main: PanelOut[];
  /** Узкие панели (рейтинг, профиль): боковая колонка. */
  side: PanelOut[];
}

/**
 * Раскладка панелей одного вуза. Порядок внутри каждой группы — серверный. На телефоне группы идут одна за
 * другой в одну колонку (лента, основное, боковое), на ноутбуке боковые панели встают справа от основных.
 */
export function layoutPanels(panels: readonly PanelOut[]): PanelLayout {
  const narrow = (panel: PanelOut) => panel.type === "ranking" || panel.type === "profile";
  return {
    band: panels.filter((panel) => panel.type === "kpi"),
    main: panels.filter((panel) => panel.type !== "kpi" && !narrow(panel)),
    side: panels.filter(narrow),
  };
}
