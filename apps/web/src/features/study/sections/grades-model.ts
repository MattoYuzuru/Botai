import type { Grade } from "@/features/data/types";

export interface GradeGroup {
  /** Название предмета; `null` — источник не назвал предмет. */
  course: string | null;
  grades: Grade[];
}

const time = (grade: Grade): number =>
  grade.gradedAt ? Date.parse(grade.gradedAt) : Number.NEGATIVE_INFINITY;

/**
 * Оценки по предметам: предметы по алфавиту, «без предмета» — последними; внутри предмета свежие первыми,
 * оценки без даты — в конце. Значения не пересчитываются и не усредняются: шкалу знает только источник.
 */
export function groupGrades(grades: readonly Grade[]): GradeGroup[] {
  const byCourse = new Map<string | null, Grade[]>();
  for (const grade of grades) {
    const key = grade.courseTitle?.trim() || null;
    byCourse.set(key, [...(byCourse.get(key) ?? []), grade]);
  }
  return [...byCourse.entries()]
    .sort(([a], [b]) => (a === null ? 1 : b === null ? -1 : a.localeCompare(b, "ru")))
    .map(([course, rows]) => ({
      course,
      grades: [...rows].sort((a, b) => time(b) - time(a) || a.id.localeCompare(b.id)),
    }));
}

/** Вес оценки так, как его назвал источник: «вес 0,3». Без процентов — они были бы выдумкой. */
export function weightLabel(weight: number | null): string | null {
  return weight === null ? null : `вес ${weight.toLocaleString("ru-RU")}`;
}
