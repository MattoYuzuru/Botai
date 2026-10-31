import type { IconName } from "@botai/design";
import type { Section } from "@/api/types";
import type { SectionKind } from "@/features/data/types";

/**
 * Вид раздела «Учёбы»: ключ экрана и вид данных источника. «Документы» — не место хранения: документы
 * показываются в карточке вуза и источника, а «Источники» живут на самом экране «Учёбы» (D-013,
 * docs/v4/03-sources-and-ux.md §5.5), поэтому строк в реестре для них нет.
 */
export type StudyKind = "courses" | "grades" | "people" | "locations" | "news";

export interface SectionDef {
  kind: StudyKind;
  title: string;
  icon: IconName;
  /** Адрес экрана-списка; пространство передаётся в `?space=`. */
  route: `/study/${StudyKind}`;
  /** Вид раздела в ответе `/spaces/{id}/sections`. */
  source: Exclude<SectionKind, "scheduleEvents" | "deadlines">;
  /** Что здесь лежит — для пустого состояния. */
  empty: { title: string; description: string };
}

/**
 * Реестр разделов «Учёбы» (styleguide §7.1): `{ kind, title, icon, route }`. Порядок фиксирован —
 * Предметы, Оценки, Преподаватели, Аудитории, Новости. Раздел появляется, только если у источников есть
 * данные этого вида; новый вид — строка сюда, а не новая вкладка.
 */
export const SECTION_REGISTRY: readonly SectionDef[] = [
  {
    kind: "courses",
    title: "Предметы",
    icon: "course",
    route: "/study/courses",
    source: "courses",
    empty: {
      title: "Предметов пока нет",
      description: "Они появятся, когда источник отдаст список предметов.",
    },
  },
  {
    kind: "grades",
    title: "Оценки",
    icon: "grade",
    route: "/study/grades",
    source: "grades",
    empty: { title: "Оценок пока нет", description: "Они появятся, когда источник отдаст оценки." },
  },
  {
    kind: "people",
    title: "Преподаватели",
    icon: "person",
    route: "/study/people",
    source: "people",
    empty: { title: "Преподавателей пока нет", description: "Они появятся, когда источник назовёт их." },
  },
  {
    kind: "locations",
    title: "Аудитории",
    icon: "room",
    route: "/study/locations",
    source: "locations",
    empty: {
      title: "Аудиторий пока нет",
      description: "Они собираются из расписания: появятся, когда в занятиях будет указана аудитория.",
    },
  },
  {
    kind: "news",
    title: "Новости",
    icon: "news",
    route: "/study/news",
    source: "news",
    empty: { title: "Новостей пока нет", description: "Они появятся, когда источник опубликует новости." },
  },
];

export const SECTION_BY_KIND: ReadonlyMap<string, SectionDef> = new Map(
  SECTION_REGISTRY.map((def) => [def.kind, def]),
);

export const isStudyKind = (value: string): value is StudyKind => SECTION_BY_KIND.has(value);

/**
 * Адреса прежних мест «Учёбы»: «Источники» и «Документы и вложения» больше не экраны (D-013,
 * 03-sources-and-ux.md §5.5). Прямая старая ссылка ведёт на «Учёбу», а не в пустоту.
 */
export const RETIRED_STUDY_KINDS: ReadonlySet<string> = new Set(["connections", "documents"]);

export interface VisibleSection {
  def: SectionDef;
  /** Сколько записей этого вида в вузе. */
  count: number;
}

/**
 * Какие разделы показать в списке вуза: виды из ответа сервера в порядке реестра, у которых есть данные.
 * Виды, которых нет в реестре (расписание, задания — у них свои вкладки), пропускаются.
 */
export function visibleSections(sections: readonly Section[]): VisibleSection[] {
  const counts = new Map(sections.map((section) => [section.kind, section.count]));
  return SECTION_REGISTRY.flatMap((def): VisibleSection[] => {
    const count = counts.get(def.source) ?? 0;
    return count > 0 ? [{ def, count }] : [];
  });
}
