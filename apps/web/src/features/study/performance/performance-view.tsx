import {
  Badge,
  Button,
  Card,
  CourseGradeCard,
  countRu,
  EmptyState,
  Icon,
  ListRow,
  Skeleton,
  StatGrid,
  StatTile,
} from "@botai/design";
import { Link } from "@tanstack/react-router";
import { useId, useMemo, useState } from "react";
import { findGradesLink } from "@/features/data/chat-links";
import type { Grade, StudyUpdate } from "@/features/data/types";
import type { Scope, SpaceDirectory } from "@/features/data/use-space-directory";
import { PanelsSection } from "../panels/panels-section";
import { CourseSheet } from "./course-sheet";
import {
  activeCourses,
  type CourseGrades,
  courseKey,
  fillOf,
  formatMax,
  groupCourses,
  headlineOf,
  NO_COURSE,
  NO_PERIOD,
  scoreLabel,
  scoreText,
  stripOf,
  summariesOf,
  zachetkaOf,
} from "./model";
import { UpdateSheets, UpdatesSection } from "./updates";

const WORKS = ["работа", "работы", "работ"] as const;
/** Сколько периодов зачётки видно сразу: текущий и прошлый; остальное — «Показать всю зачётку». */
const ZACHETKA_PERIODS = 2;

export interface PerformanceViewProps {
  scope: Scope;
  directory: SpaceDirectory;
  nowMs: number;
  grades: readonly Grade[];
  updates: readonly StudyUpdate[];
  /** Оценки ещё не пришли: показываем скелетон, а не «оценок нет». */
  loading: boolean;
  /** У области есть панели D-039 или предложения агента — пустого состояния тогда нет. */
  hasPanels: boolean;
}

/**
 * «Успеваемость» (D-046): «Новое», предметы с итогом или накопленным как дал источник и полосой последних работ,
 * «Зачётка» (итоги по периодам), «Показатели» (сводные показатели источника) и панели D-039. Телефон — одна
 * колонка; ноутбук — предметы слева сеткой, «Новое» справа. Приложение ничего не
 * считает за источник: значения, шкалы и периоды — его слова. Ноутбук — две строки сетки: предметы и «Новое»,
 * затем «Показатели» и «Зачётка»; телефон — одна колонка в том же порядке, что и в разметке.
 */
export function PerformanceView({
  scope,
  directory,
  nowMs,
  grades,
  updates,
  loading,
  hasPanels,
}: PerformanceViewProps) {
  const [openKey, setOpenKey] = useState<string | null>(null);
  const [openUpdate, setOpenUpdate] = useState<StudyUpdate | null>(null);
  const courses = useMemo(() => groupCourses(grades), [grades]);
  const cards = useMemo(() => activeCourses(courses), [courses]);
  const zachetka = useMemo(() => zachetkaOf(grades), [grades]);
  const summaries = useMemo(() => summariesOf(grades), [grades]);
  const showSpace = scope.isAll && directory.spaces.length > 1;
  const opened = courses.find((course) => course.key === openKey) ?? null;
  const empty = !loading && grades.length === 0 && updates.length === 0;
  const single = scope.isAll ? undefined : scope.spaces[0]?.title;

  return (
    <div data-view="performance" className="flex min-w-0 flex-col gap-8">
      {loading ? (
        <PerformanceSkeleton />
      ) : empty && !hasPanels ? (
        <EmptyState
          icon="grade"
          title="Оценок пока нет"
          description="Когда источники отдадут оценки — баллы за работы, накопленные и итоговые, — они появятся здесь по предметам. Агент может поискать их сам."
          action={
            <Button variant="primary" leadingIcon="chat" asChild>
              <Link {...findGradesLink(single)}>Найти оценки</Link>
            </Button>
          }
        />
      ) : (
        <div className="grid min-w-0 grid-cols-1 items-start gap-8 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)] lg:gap-x-6">
          <div className="min-w-0 lg:col-start-2 lg:row-start-1">
            <UpdatesSection
              updates={updates}
              spaces={directory.byId}
              nowMs={nowMs}
              showSpace={showSpace}
              caption="за неделю"
              onOpen={setOpenUpdate}
            />
          </div>
          {cards.length > 0 ? (
            <CoursesBlock
              courses={cards}
              showSpace={showSpace}
              directory={directory}
              onOpen={(course) => setOpenKey(course.key)}
            />
          ) : (
            <EmptyState
              icon="course"
              size="sm"
              layout="inline"
              as="h2"
              title="Оценок за работы пока нет"
              description="Здесь появятся предметы с баллами за работы и накопленным результатом."
              className="lg:col-start-1 lg:row-start-1"
            />
          )}
          {summaries.length > 0 && (
            <SummaryBlock summaries={summaries} showSpace={showSpace} directory={directory} />
          )}
          {zachetka.length > 0 && (
            <ZachetkaBlock
              periods={zachetka}
              showSpace={showSpace}
              directory={directory}
              besideCourses={updates.length === 0}
              onOpen={(grade) => setOpenKey(courseKey(grade))}
            />
          )}
        </div>
      )}

      <PanelsSection scope={scope} title="Панели" />

      <CourseSheet course={opened} spaces={directory.byId} nowMs={nowMs} onClose={() => setOpenKey(null)} />
      <UpdateSheets
        update={openUpdate}
        spaceId={scope.spaceId}
        spaces={directory.byId}
        nowMs={nowMs}
        onClose={() => setOpenUpdate(null)}
      />
    </div>
  );
}

function CoursesBlock({
  courses,
  showSpace,
  directory,
  onOpen,
}: {
  courses: readonly CourseGrades[];
  showSpace: boolean;
  directory: SpaceDirectory;
  onOpen: (course: CourseGrades) => void;
}) {
  const headingId = useId();
  return (
    <section
      aria-labelledby={headingId}
      data-part="courses"
      className="flex min-w-0 flex-col gap-3 lg:col-start-1 lg:row-start-1"
    >
      <div className="flex items-baseline justify-between gap-3 px-1">
        <h2 id={headingId} className="type-heading text-fg">
          Предметы
        </h2>
        <span className="type-caption text-fg-muted">{courses.length}</span>
      </div>
      <ul className="grid min-w-0 grid-cols-1 gap-2 md:grid-cols-2 lg:gap-3">
        {courses.map((course) => {
          const headline = headlineOf(course);
          const text = headline ? scoreText(headline.grade) : null;
          const space = directory.byId.get(course.spaceId);
          return (
            <li key={course.key} className="min-w-0">
              <CourseGradeCard
                title={course.title ?? NO_COURSE}
                headline={
                  headline && text
                    ? {
                        label: headline.label,
                        value: text.value,
                        suffix: text.suffix,
                        period: headline.label === "Последняя оценка" ? null : headline.grade.period,
                        fill: headline.label === "Последняя оценка" ? null : fillOf(headline.grade),
                      }
                    : null
                }
                caption={
                  course.items.length > 0
                    ? `Проверено: ${countRu(course.items.length, WORKS)}`
                    : "Работ с оценкой пока нет"
                }
                strip={stripOf(course.items)}
                badge={showSpace && space ? <Badge tone="neutral">{space.shortTitle}</Badge> : undefined}
                onOpen={() => onOpen(course)}
                // Карточки одной строки сетки — одной высоты: ряд заканчивается ровно.
                className="h-full"
              />
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function SummaryBlock({
  summaries,
  showSpace,
  directory,
}: {
  summaries: readonly Grade[];
  showSpace: boolean;
  directory: SpaceDirectory;
}) {
  const headingId = useId();
  return (
    <section
      aria-labelledby={headingId}
      data-part="summary"
      className="flex min-w-0 flex-col gap-3 lg:col-start-1 lg:row-start-2"
    >
      <h2 id={headingId} className="px-1 type-heading text-fg">
        Показатели
      </h2>
      <StatGrid columns={3} label="Сводные показатели">
        {summaries.map((grade) => {
          const space = directory.byId.get(grade.provenance.spaceId);
          const max = grade.scaleMax ?? null;
          const caption = [grade.period, showSpace ? space?.shortTitle : null].filter(Boolean).join(" · ");
          return (
            <StatTile
              key={grade.id}
              label={grade.title?.trim() || "Показатель"}
              value={grade.value}
              scale={max !== null ? formatMax(max) : null}
              unit={max === null ? grade.scale : null}
              caption={caption || undefined}
              compact
            />
          );
        })}
      </StatGrid>
    </section>
  );
}

function ZachetkaBlock({
  periods,
  showSpace,
  directory,
  besideCourses,
  onOpen,
}: {
  periods: ReturnType<typeof zachetkaOf>;
  /** «Нового» нет: на ноутбуке зачётка встаёт справа от предметов, а не под пустым местом. */
  besideCourses: boolean;
  showSpace: boolean;
  directory: SpaceDirectory;
  onOpen: (grade: Grade) => void;
}) {
  const headingId = useId();
  const listId = useId();
  const [all, setAll] = useState(false);
  const shown = all ? periods : periods.slice(0, ZACHETKA_PERIODS);
  const hidden = periods.slice(ZACHETKA_PERIODS).reduce((sum, period) => sum + period.grades.length, 0);
  return (
    <section
      aria-labelledby={headingId}
      data-part="zachetka"
      className={`flex min-w-0 flex-col gap-3 lg:col-start-2 ${besideCourses ? "lg:row-start-1" : "lg:row-start-2"}`}
    >
      <h2 id={headingId} className="px-1 type-heading text-fg">
        Зачётка
      </h2>
      <Card padding="sm" className="flex flex-col gap-2" id={listId}>
        {shown.map((period, index) => {
          const periodId = `${headingId}-${index}`;
          return (
            <section
              key={`${period.spaceId}|${period.period ?? ""}`}
              aria-labelledby={periodId}
              className="flex flex-col"
            >
              <h3 id={periodId} className="px-3 pt-2 pb-1 type-label text-fg-muted">
                {showSpace
                  ? `${directory.byId.get(period.spaceId)?.shortTitle ?? ""} · ${period.period ?? NO_PERIOD}`
                  : (period.period ?? NO_PERIOD)}
              </h3>
              <ul className="flex flex-col">
                {period.grades.map((grade) => (
                  <li key={grade.id}>
                    <ListRow
                      title={grade.courseTitle ?? grade.title ?? NO_COURSE}
                      trailing={<span className="type-subheading text-fg">{scoreLabel(grade)}</span>}
                      onOpen={() => onOpen(grade)}
                    />
                  </li>
                ))}
              </ul>
            </section>
          );
        })}
        {hidden > 0 && (
          <Button
            variant="ghost"
            className="self-start"
            aria-expanded={all}
            aria-controls={listId}
            onClick={() => setAll((value) => !value)}
          >
            {/* Без счётчика: на 320 px подпись кнопки должна умещаться в одну строку. */}
            {all ? "Свернуть зачётку" : "Показать всю зачётку"}
            <Icon name={all ? "chevron-up" : "chevron-down"} size={20} />
          </Button>
        )}
      </Card>
    </section>
  );
}

function PerformanceSkeleton() {
  return (
    <div role="status" aria-busy="true" className="flex flex-col gap-3" data-part="performance-loading">
      <span className="sr-only">Загрузка оценок</span>
      <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
        {[0, 1, 2, 3].map((row) => (
          <Skeleton key={row} className="h-28 rounded-lg" />
        ))}
      </div>
    </div>
  );
}
