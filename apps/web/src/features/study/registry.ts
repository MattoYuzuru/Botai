import type { IconName } from "@botai/design";
import type { Section } from "@/api/types";
import type { SectionKind } from "@/features/data/types";

/** Вид раздела «Учёбы»: ключ экрана. Это не всегда `kind` API: «Источники» есть у любого пространства. */
export type StudyKind = "courses" | "grades" | "people" | "locations" | "news" | "documents" | "connections";

export interface SectionDef {
  kind: StudyKind;
  title: string;
  icon: IconName;
  /** Адрес экрана-списка; пространство передаётся в `?space=`. */
  route: `/study/${StudyKind}`;
  /**
   * Вид раздела в ответе `/spaces/{id}/sections`. `null` — у раздела нет данных такого вида (Источники): в списке
   * разделов вуза его строки нет, источники показаны на самом экране «Учёба», а адрес остаётся для управления.
   */
  source: SectionKind | null;
  /** Что здесь лежит — для пустого состояния. */
  empty: { title: string; description: string };
}

/**
 * Реестр разделов «Учёбы» (styleguide §7.1): `{ kind, title, icon, route }`. Порядок фиксирован — Предметы,
 * Оценки, Преподаватели, Аудитории, Новости, Документы и вложения, Источники (управление). Раздел появляется,
 * только если у источников есть данные этого вида; новый вид добавляет строку сюда, а не новую вкладку.
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
  {
    kind: "documents",
    title: "Документы и вложения",
    icon: "document",
    route: "/study/documents",
    source: "documents",
    empty: {
      title: "Документов пока нет",
      description: "Добавьте ссылку или файл здесь или пришлите их в чат — BotAI прочитает их.",
    },
  },
  {
    kind: "connections",
    title: "Источники",
    icon: "connection",
    route: "/study/connections",
    source: null,
    empty: { title: "Источников пока нет", description: "Добавьте источник через чат — он появится здесь." },
  },
];

export const SECTION_BY_KIND: ReadonlyMap<string, SectionDef> = new Map(
  SECTION_REGISTRY.map((def) => [def.kind, def]),
);

export const isStudyKind = (value: string): value is StudyKind => SECTION_BY_KIND.has(value);

export interface VisibleSection {
  def: SectionDef;
  /** Сколько записей этого вида в вузе. */
  count: number;
}

/**
 * Какие разделы показать в списке вуза: виды из ответа сервера в порядке реестра, у которых есть данные.
 * Виды, которых нет в реестре (расписание, задания — у них свои вкладки), и «Источники» пропускаются.
 */
export function visibleSections(sections: readonly Section[]): VisibleSection[] {
  const counts = new Map(sections.map((section) => [section.kind, section.count]));
  return SECTION_REGISTRY.flatMap((def): VisibleSection[] => {
    if (def.source === null) return [];
    const count = counts.get(def.source) ?? 0;
    return count > 0 ? [{ def, count }] : [];
  });
}
