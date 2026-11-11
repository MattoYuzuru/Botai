import { Badge, Button, formatDay, Icon, ScoreBar, Sheet, SourceCitation, safeHttpUrl } from "@botai/design";
import { Link } from "@tanstack/react-router";
import { type ReactNode, type RefObject, useEffect, useRef } from "react";
import { askCourseDraft, chatDraftLink } from "@/features/data/chat-links";
import { openUrlOf } from "@/features/data/open-url";
import type { Grade } from "@/features/data/types";
import type { SpaceInfo } from "@/features/data/use-space-directory";
import { type CourseGrades, fillOf, gradeTime, NO_COURSE, scoreText, weightLabel } from "./model";

export interface CourseSheetProps {
  /** Открытый предмет; `null` — шит закрыт. Содержимое остаётся на время анимации закрытия. */
  course: CourseGrades | null;
  /** Запись, ради которой открыли шит (из «Нового»): подсвечена и прокручена в вид. */
  focusId?: string | null;
  spaces: ReadonlyMap<string, SpaceInfo>;
  nowMs: number;
  onClose: () => void;
}

/**
 * Карточка предмета (D-046): «Итог», «Накоплено» и «Работы» — значения и шкалы источника, полоса заполнения
 * одной записи, вес и комментарий проверяющего, если источник их дал, и «Открыть в источнике» (значок в строке) —
 * страница этой оценки, иначе страница источника. Ничего не суммируется и не усредняется (D-039).
 */
export function CourseSheet({ course, focusId, spaces, nowMs, onClose }: CourseSheetProps) {
  const last = useRef<CourseGrades | null>(null);
  if (course) last.current = course;
  const shown = course ?? last.current;
  const space = shown ? spaces.get(shown.spaceId) : undefined;
  return (
    <Sheet
      open={course !== null}
      onOpenChange={(open) => !open && onClose()}
      title={shown?.title ?? NO_COURSE}
      description={space?.title}
    >
      {shown && <CourseDetails course={shown} focusId={focusId ?? null} space={space} nowMs={nowMs} />}
    </Sheet>
  );
}

function CourseDetails({
  course,
  focusId,
  space,
  nowMs,
}: {
  course: CourseGrades;
  focusId: string | null;
  space: SpaceInfo | undefined;
  nowMs: number;
}) {
  const focused = useRef<HTMLLIElement | null>(null);
  useEffect(() => {
    if (focusId) focused.current?.scrollIntoView({ block: "nearest" });
  }, [focusId]);
  const all = [...course.finals, ...course.current, ...course.results, ...course.items];
  const latest = all.reduce<Grade | null>(
    (best, grade) => (best === null || gradeTime(grade) > gradeTime(best) ? grade : best),
    null,
  );
  const connection = latest
    ? space?.connections.find((row) => row.id === latest.provenance.connectionId)
    : undefined;
  const rowProps = { space, nowMs, focusId, focusedRef: focused, courseTitle: course.title };

  return (
    <div className="flex flex-col gap-5 pb-2">
      {course.finals.length > 0 && (
        <GradeGroup title="Итог" id="course-finals">
          {course.finals.map((grade) => (
            <GradeEntry key={grade.id} grade={grade} {...rowProps} headline />
          ))}
        </GradeGroup>
      )}
      {course.current.length > 0 && (
        <GradeGroup
          title="Накоплено"
          id="course-current"
          note="Текущий результат по предмету — так его считает источник."
        >
          {course.current.map((grade) => (
            <GradeEntry key={grade.id} grade={grade} {...rowProps} headline />
          ))}
        </GradeGroup>
      )}
      {course.results.length > 0 && (
        <GradeGroup
          title="Результат"
          id="course-results"
          note={
            course.results.length > 1
              ? "Оценки за курс, как их дал источник. Какая из них итоговая, источник не сказал."
              : "Оценка за курс, как её дал источник; итог это или накопленное, источник не сказал."
          }
        >
          {course.results.map((grade) => (
            <GradeEntry key={grade.id} grade={grade} {...rowProps} headline />
          ))}
        </GradeGroup>
      )}
      {course.items.length > 0 && (
        <GradeGroup title="Работы" id="course-items" count={course.items.length}>
          {course.items.map((grade) => (
            <GradeEntry key={grade.id} grade={grade} {...rowProps} />
          ))}
        </GradeGroup>
      )}

      {latest && (
        <SourceCitation
          title={connection?.title ?? "Оценки"}
          url={openUrlOf(latest.provenance, connection)}
          fetchedAt={Date.parse(latest.provenance.fetchedAt)}
          nowMs={nowMs}
          timeZone={space?.timeZone}
          spaceId={latest.provenance.spaceId}
          spaceTitle={space?.shortTitle}
          stale={latest.provenance.stale}
        />
      )}
      <Button variant="ghost" leadingIcon="chat" asChild className="self-start">
        <Link {...chatDraftLink(askCourseDraft(course.title, space?.title))}>Спросить агента</Link>
      </Button>
    </div>
  );
}

function GradeGroup({
  title,
  id,
  note,
  count,
  children,
}: {
  title: string;
  id: string;
  note?: string;
  count?: number;
  children: ReactNode;
}) {
  return (
    <section aria-labelledby={id} className="flex flex-col gap-2">
      <h3 id={id} className="flex items-baseline gap-2 type-label text-fg-muted">
        {title}
        {count !== undefined && <span className="type-caption text-fg-subtle">{count}</span>}
      </h3>
      {note && <p className="type-caption text-fg-subtle">{note}</p>}
      <ul className="flex flex-col divide-y divide-border rounded-lg border border-border">{children}</ul>
    </section>
  );
}

function GradeEntry({
  grade,
  space,
  nowMs,
  focusId,
  focusedRef,
  headline = false,
  courseTitle,
}: {
  /** Название карточки: строка не повторяет его своим заголовком. */
  courseTitle: string | null;
  grade: Grade;
  space: SpaceInfo | undefined;
  nowMs: number;
  focusId: string | null;
  focusedRef: RefObject<HTMLLIElement | null>;
  headline?: boolean;
}) {
  const { value, suffix, caption } = scoreText(grade);
  const fill = fillOf(grade);
  const connection = space?.connections.find((row) => row.id === grade.provenance.connectionId);
  const href = safeHttpUrl(grade.url)?.href ?? openUrlOf(grade.provenance, connection);
  const when = grade.gradedAt ? Date.parse(grade.gradedAt) : null;
  const weight = weightLabel(grade.weight);
  const isFocused = focusId === grade.id;
  const ownTitle = grade.title?.trim() && grade.title.trim() !== courseTitle ? grade.title.trim() : null;
  const title = headline ? (grade.period ?? ownTitle) : (grade.title ?? "Работа");
  return (
    <li
      ref={isFocused ? focusedRef : undefined}
      data-grade-id={grade.id}
      data-focused={isFocused || undefined}
      className={`flex flex-col gap-2 p-3 first:rounded-t-lg last:rounded-b-lg ${isFocused ? "bg-accent-subtle" : ""}`}
    >
      <div className="flex items-start gap-3">
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          {title && <p className="break-words type-subheading text-fg">{title}</p>}
          <p className="flex flex-wrap items-center gap-x-2 gap-y-1 type-caption text-fg-muted">
            {when !== null && (
              <time dateTime={grade.gradedAt ?? undefined}>{formatDay(when, nowMs, space?.timeZone)}</time>
            )}
            {weight && <span>{weight}</span>}
            {caption && <span className="min-w-0 max-w-full truncate">{caption}</span>}
            {isFocused && <Badge tone="accent">Новое</Badge>}
          </p>
        </div>
        <p className="flex max-w-[50%] shrink-0 flex-wrap items-baseline justify-end gap-x-1 break-words text-right">
          <span className={headline ? "type-title text-fg" : "type-subheading text-fg"} data-numeric="">
            {value}
          </span>
          {suffix && (
            <span className="type-caption text-fg-muted" data-numeric="">
              {suffix}
            </span>
          )}
        </p>
        {href && (
          // Значок вместо подписи в каждой строке: список работ остаётся плотным; имя ссылки — полное.
          <Button variant="ghost" asChild className="-my-2 -mr-2 size-11 shrink-0 px-0">
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              aria-label={`Открыть в источнике: ${title ?? "оценка"}`}
            >
              <Icon name="external" size={20} />
            </a>
          </Button>
        )}
      </div>
      {fill !== null && <ScoreBar fill={fill} />}
      {grade.comment?.trim() && (
        <figure className="flex gap-2 rounded-md bg-bg-subtle p-3">
          <Icon name="chat" size={16} className="shrink-0 text-fg-subtle" />
          <div className="flex min-w-0 flex-col gap-1">
            <figcaption className="type-caption text-fg-muted">Комментарий проверяющего</figcaption>
            <blockquote className="whitespace-pre-line break-words type-ui text-fg">
              {grade.comment.trim()}
            </blockquote>
          </div>
        </figure>
      )}
    </li>
  );
}
