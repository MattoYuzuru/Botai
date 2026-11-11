import { fillOf, scoreLabel } from "@/features/data/scores";
import type { Grade, GradeLayer, StudyUpdate } from "@/features/data/types";

export { fillOf, formatMax, type ScoreText, scoreLabel, scoreText } from "@/features/data/scores";

/**
 * Модель «Успеваемости» (D-043, D-046, docs/v4/27-study-hub-grades-fastpath.md §3.1, §6). Оценки раскладываются
 * по слоям источника: работа (`item`), накопленное (`current`), итог (`final`), сводный показатель (`summary`).
 * Приложение ничего не считает за источник (D-039): нет средних, сумм и пересчёта шкал. Единственная
 * производная величина — доля `valueNum / scaleMax` одной записи для полосы заполнения: это та же пара чисел
 * источника.
 */

/** Слой записи; `null` (источник не сказал) показывается как оценка за работу. */
export const layerOf = (grade: Pick<Grade, "layer">): GradeLayer => grade.layer ?? "item";

/** Время записи для порядка: когда выставлена, иначе когда изменилась, иначе когда прочитана. */
export function gradeTime(grade: Pick<Grade, "gradedAt" | "changedAt" | "provenance">): number {
  for (const value of [grade.gradedAt, grade.changedAt, grade.provenance.fetchedAt]) {
    const at = value ? Date.parse(value) : Number.NaN;
    if (Number.isFinite(at)) return at;
  }
  return Number.NEGATIVE_INFINITY;
}

const newestFirst = (a: Grade, b: Grade) => gradeTime(b) - gradeTime(a) || a.id.localeCompare(b.id);

/** Вес так, как его назвал источник: «вес 0,3». Без процентов — они были бы выдумкой. */
export const weightLabel = (weight: number | null): string | null =>
  weight === null ? null : `вес ${weight.toLocaleString("ru-RU")}`;

// --- предметы ------------------------------------------------------------------------------------------

export const NO_COURSE = "Без предмета";

export interface CourseGrades {
  /** Ключ предмета: вуз + id предмета или название (одноимённые предметы двух вузов — разные). */
  key: string;
  /** Название предмета; `null` — источник предмет не назвал («Без предмета»). */
  title: string | null;
  spaceId: string;
  /** Оценки за работы, свежие первыми. */
  items: Grade[];
  /** Накопленное по предмету, свежее первым. */
  current: Grade[];
  /** Итоги по предмету (ведомость, зачётка), свежие первыми. */
  finals: Grade[];
  /** Самое свежее время среди записей предмета. */
  latestAt: number;
}

export function courseKey(grade: Pick<Grade, "courseId" | "courseTitle" | "provenance">): string {
  const title = grade.courseTitle?.trim();
  return `${grade.provenance.spaceId}:${grade.courseId ?? (title ? `t:${title.toLowerCase()}` : "none")}`;
}

/**
 * Предметы со всеми слоями оценок. Сводные показатели (`summary`) к предмету не относятся и сюда не входят.
 * Свежие предметы первыми; «Без предмета» — в конце.
 */
export function groupCourses(grades: readonly Grade[]): CourseGrades[] {
  const byKey = new Map<string, CourseGrades>();
  for (const grade of grades) {
    const layer = layerOf(grade);
    if (layer === "summary") continue;
    const key = courseKey(grade);
    let course = byKey.get(key);
    if (!course) {
      course = {
        key,
        title: grade.courseTitle?.trim() || null,
        spaceId: grade.provenance.spaceId,
        items: [],
        current: [],
        finals: [],
        latestAt: Number.NEGATIVE_INFINITY,
      };
      byKey.set(key, course);
    }
    (layer === "final" ? course.finals : layer === "current" ? course.current : course.items).push(grade);
    course.latestAt = Math.max(course.latestAt, gradeTime(grade));
  }
  const courses = [...byKey.values()].map((course) => ({
    ...course,
    items: course.items.sort(newestFirst),
    current: course.current.sort(newestFirst),
    finals: course.finals.sort(newestFirst),
  }));
  return courses.sort((a, b) => {
    if ((a.title === null) !== (b.title === null)) return a.title === null ? 1 : -1;
    return (
      b.latestAt - a.latestAt ||
      (a.title ?? "").localeCompare(b.title ?? "", "ru") ||
      a.key.localeCompare(b.key)
    );
  });
}

/**
 * Карточки предметов текущей учёбы: предметы с работами или накопленным. Предмет только с итогом — это зачётка
 * прошлых периодов, он живёт в блоке «Зачётка», а не отдельной карточкой.
 */
export const activeCourses = (courses: readonly CourseGrades[]): CourseGrades[] =>
  courses.filter((course) => course.items.length > 0 || course.current.length > 0);

export interface Headline {
  /** «Итог» — итог источника; «Накоплено» — текущий результат; «Последняя оценка» — свежая работа. */
  label: "Итог" | "Накоплено" | "Последняя оценка";
  grade: Grade;
}

/**
 * Главный результат предмета — как его дал источник: свежий итог, иначе накопленное, иначе последняя работа.
 * Ничего не складывается и не усредняется.
 */
export function headlineOf(course: CourseGrades): Headline | null {
  const [final] = course.finals;
  if (final) return { label: "Итог", grade: final };
  const [current] = course.current;
  if (current) return { label: "Накоплено", grade: current };
  const [latest] = course.items;
  return latest ? { label: "Последняя оценка", grade: latest } : null;
}

export interface StripItem {
  id: string;
  /** Доля заполнения 0…1 или `null` — у оценки нет числа и максимума: показывается текстом. */
  fill: number | null;
  /** Значение словами: «8 из 10», «зачтено». */
  text: string;
  /** За что оценка — для подписи скринридеру. */
  title: string | null;
}

export const STRIP_SIZE = 8;

/** Полоса последних оценок за работы: до `size` штук, слева старые, справа свежие. */
export function stripOf(items: readonly Grade[], size = STRIP_SIZE): StripItem[] {
  return items
    .slice(0, size)
    .reverse()
    .map((grade) => ({ id: grade.id, fill: fillOf(grade), text: scoreLabel(grade), title: grade.title }));
}

// --- зачётка и показатели -------------------------------------------------------------------------------

export const NO_PERIOD = "Без периода";

export interface ZachetkaPeriod {
  /** Вуз периода: периоды у вузов свои («1 модуль» ВШЭ и ЦУ — разные). */
  spaceId: string;
  /** Период, как назвал источник; `null` — «Без периода». */
  period: string | null;
  grades: Grade[];
}

/**
 * «Зачётка»: итоги (`final`) по периодам каждого вуза. Периоды — самые свежие первыми (по времени их записей),
 * «Без периода» — последним; внутри периода предметы по алфавиту.
 */
export function zachetkaOf(grades: readonly Grade[]): ZachetkaPeriod[] {
  const byPeriod = new Map<string, ZachetkaPeriod>();
  for (const grade of grades) {
    if (layerOf(grade) !== "final") continue;
    const period = grade.period?.trim() || null;
    const key = `${grade.provenance.spaceId}|${period ?? ""}`;
    const group = byPeriod.get(key) ?? { spaceId: grade.provenance.spaceId, period, grades: [] };
    group.grades.push(grade);
    byPeriod.set(key, group);
  }
  const latest = (rows: readonly Grade[]) => Math.max(...rows.map(gradeTime));
  return [...byPeriod.values()]
    .sort((a, b) => {
      if ((a.period === null) !== (b.period === null)) return a.period === null ? 1 : -1;
      return latest(b.grades) - latest(a.grades) || (b.period ?? "").localeCompare(a.period ?? "", "ru");
    })
    .map((group) => ({
      ...group,
      grades: [...group.grades].sort(
        (x, y) =>
          (x.courseTitle ?? x.title ?? "").localeCompare(y.courseTitle ?? y.title ?? "", "ru") ||
          x.id.localeCompare(y.id),
      ),
    }));
}

/** Сводные показатели (`summary`): GPA, средний балл, рейтинг — как дал источник, свежие первыми. */
export const summariesOf = (grades: readonly Grade[]): Grade[] =>
  grades.filter((grade) => layerOf(grade) === "summary").sort(newestFirst);

// --- «Новое» -------------------------------------------------------------------------------------------

export const UPDATES_SHOWN = 5;
/** На телефоне «Новое» короче: предметы должны попасть на первый экран. */
export const UPDATES_SHOWN_PHONE = 3;

/** Подпись записи «Новое» без значения: что именно изменилось. */
export function updateTitle(update: StudyUpdate): string {
  if (update.title?.trim()) return update.title.trim();
  if (update.kind === "deadline") return "Задание";
  if (update.layer === "final") return "Итог по предмету";
  if (update.layer === "current") return "Накопленная оценка";
  if (update.layer === "summary") return "Сводный показатель";
  return "Оценка";
}

/** Свежие первыми; одинаковое время — по id, чтобы порядок не прыгал между опросами. */
export const sortUpdates = (updates: readonly StudyUpdate[]): StudyUpdate[] =>
  [...updates].sort((a, b) => Date.parse(b.changedAt) - Date.parse(a.changedAt) || a.id.localeCompare(b.id));

/** Оценка, восстановленная из записи «Новое», — когда в списке оценок её ещё нет (опрос не успел). */
export function gradeFromUpdate(update: StudyUpdate): Grade {
  return {
    id: update.id,
    title: update.title,
    value: update.value ?? "",
    scale: update.scale,
    weight: null,
    courseTitle: update.courseTitle,
    gradedAt: null,
    provenance: update.provenance,
    valueNum: update.valueNum,
    scaleMax: update.scaleMax,
    layer: update.layer,
    url: update.url,
    changedAt: update.changedAt,
  };
}
