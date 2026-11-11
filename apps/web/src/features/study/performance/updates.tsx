import { Badge, Button, Card, formatAgo, Icon, UpdateRow, useMediaQuery } from "@botai/design";
import { useQuery } from "@tanstack/react-query";
import { useId, useMemo, useState } from "react";
import { deadlinesQuery, gradesQuery } from "@/features/data/queries";
import { scoreLabel } from "@/features/data/scores";
import type { StudyUpdate } from "@/features/data/types";
import type { SpaceInfo } from "@/features/data/use-space-directory";
import { DeadlineSheet } from "@/features/tasks/deadline-sheet";
import { DEADLINE_STATE_LABEL, toDeadlineView } from "@/features/tasks/deadlines";
import { CourseSheet } from "./course-sheet";
import {
  courseKey,
  gradeFromUpdate,
  groupCourses,
  sortUpdates,
  UPDATES_SHOWN,
  UPDATES_SHOWN_PHONE,
  updateTitle,
} from "./model";

const DONE = new Set(["submitted", "graded"]);

/**
 * Блок «Новое» (D-044 §5.5, D-046): новые оценки и смены статуса заданий, свежие первыми. Первые `shown`
 * записей, «Показать все» раскрывает остальные. Пустой список — блока нет (это не «ничего не изменилось
 * навсегда», а просто пустая неделя). Нажатие открывает тот же шит, что и у исходной записи.
 */
export function UpdatesSection({
  updates,
  spaces,
  nowMs,
  showSpace,
  shown: shownProp,
  caption,
  onOpen,
}: {
  updates: readonly StudyUpdate[];
  spaces: ReadonlyMap<string, SpaceInfo>;
  nowMs: number;
  showSpace: boolean;
  /** Сколько записей видно сразу; по умолчанию — 3 на телефоне (не вытеснять предметы с первого экрана), 5 шире. */
  shown?: number;
  /** Окно «Нового» словами: «за неделю», «за три дня». */
  caption: string;
  onOpen: (update: StudyUpdate) => void;
}) {
  const headingId = useId();
  const listId = useId();
  const [expanded, setExpanded] = useState(false);
  const rows = useMemo(() => sortUpdates(updates), [updates]);
  const wide = useMediaQuery("(min-width: 1024px)");
  if (rows.length === 0) return null;
  const shown = shownProp ?? (wide ? UPDATES_SHOWN : UPDATES_SHOWN_PHONE);
  const visible = expanded ? rows : rows.slice(0, shown);
  return (
    <section aria-labelledby={headingId} data-part="updates" className="flex min-w-0 flex-col gap-3">
      <div className="flex items-baseline justify-between gap-3 px-1">
        <h2 id={headingId} className="type-heading text-fg">
          Новое
        </h2>
        <span className="type-caption text-fg-muted">{caption}</span>
      </div>
      <Card padding="sm" className="flex flex-col">
        <ul id={listId} className="flex flex-col">
          {visible.map((update) => {
            const space = spaces.get(update.provenance.spaceId);
            const done = update.kind === "deadline" && update.state !== null && DONE.has(update.state);
            const status =
              update.kind === "deadline" && update.state ? (
                <Badge tone={done ? "success" : "neutral"} icon={done ? "ok" : undefined}>
                  {update.stateText?.trim() || DEADLINE_STATE_LABEL[update.state]}
                </Badge>
              ) : null;
            const value = update.value?.trim()
              ? scoreLabel({ value: update.value, scale: update.scale, scaleMax: update.scaleMax })
              : null;
            return (
              <li key={`${update.kind}:${update.id}`}>
                <UpdateRow
                  kind={update.kind}
                  done={done}
                  title={updateTitle(update)}
                  value={value}
                  status={status}
                  meta={
                    // Одна строка: вуз и предмет обрезаются многоточием, время остаётся всегда.
                    <span className="flex w-full min-w-0 gap-1">
                      <span className="min-w-0 truncate">
                        {[showSpace ? space?.shortTitle : null, update.courseTitle]
                          .filter(Boolean)
                          .join(" · ")}
                      </span>
                      <time dateTime={update.changedAt} className="shrink-0">
                        {showSpace || update.courseTitle ? "· " : ""}
                        {formatAgo(Date.parse(update.changedAt), nowMs, space?.timeZone)}
                      </time>
                    </span>
                  }
                  onOpen={() => onOpen(update)}
                />
              </li>
            );
          })}
        </ul>
        {rows.length > shown && (
          <Button
            variant="ghost"
            className="self-start"
            aria-expanded={expanded}
            aria-controls={listId}
            onClick={() => setExpanded((value) => !value)}
          >
            {expanded ? "Свернуть" : `Показать все (${rows.length})`}
            <Icon name={expanded ? "chevron-up" : "chevron-down"} size={20} />
          </Button>
        )}
      </Card>
    </section>
  );
}

/**
 * Шиты записей «Нового»: оценка открывает карточку своего предмета с подсвеченной работой, задание — карточку
 * задания. Если опрос ещё не принёс запись в списки, шит строится из самой записи «Нового».
 */
export function UpdateSheets({
  update,
  spaceId,
  spaces,
  nowMs,
  onClose,
}: {
  update: StudyUpdate | null;
  /** Область списков, из которых берутся полные записи (`undefined` — все вузы). */
  spaceId: string | undefined;
  spaces: ReadonlyMap<string, SpaceInfo>;
  nowMs: number;
  onClose: () => void;
}) {
  const grades = useQuery({ ...gradesQuery(spaceId), enabled: update?.kind === "grade" });
  const deadlines = useQuery({ ...deadlinesQuery(spaceId, true), enabled: update?.kind === "deadline" });

  const course = useMemo(() => {
    if (update?.kind !== "grade") return null;
    const all = grades.data?.data ?? [];
    const known = all.find((grade) => grade.id === update.id);
    const pool = known ? all : [...all, gradeFromUpdate(update)];
    const key = courseKey(known ?? gradeFromUpdate(update));
    return groupCourses(pool).find((row) => row.key === key) ?? null;
  }, [update, grades.data]);

  const deadline = useMemo(() => {
    if (update?.kind !== "deadline") return null;
    const known = deadlines.data?.data.find((row) => row.id === update.id);
    return toDeadlineView(
      known ?? {
        id: update.id,
        title: update.title ?? "Задание",
        dueAt: null,
        effectiveDueAt: null,
        state: update.state ?? "unknown",
        stateText: update.stateText,
        stateSource: "source",
        courseTitle: update.courseTitle,
        url: update.url,
        provenance: update.provenance,
      },
      nowMs,
    );
  }, [update, deadlines.data, nowMs]);

  return (
    <>
      <CourseSheet
        course={course}
        focusId={update?.kind === "grade" ? update.id : null}
        spaces={spaces}
        nowMs={nowMs}
        onClose={onClose}
      />
      <DeadlineSheet view={deadline} spaces={spaces} nowMs={nowMs} onClose={onClose} />
    </>
  );
}
