import { Badge, Button, EmptyState, formatAgo, Icon, Sheet, SkeletonText } from "@botai/design";
import { Link } from "@tanstack/react-router";
import { type SpaceInfo, useSpaceDirectory } from "@/features/data/use-space-directory";
import { sourceChatLinks } from "@/features/study/v4/chat-links-v4";
import { describeSourceKind, describeSourceStatus, kindsLabel, sourceIcon } from "@/features/study/v4/status";
import { type StudySource, useStudySources } from "@/features/study/v4/use-study-sources";
import { addSourceDraft } from "./drafts";

/**
 * Панель «Источники» (§3.9): вузы пользователя и их источники со статусами — только чтение, те же данные
 * v4, что на «Учёбе» и «Сегодня» (`/api/v1/sources`, с fallback на прежние подключения, если v4-API
 * на сервере выключен). Источник с тредом задачи ведёт в него; «Добавить источник» открывает новый тред
 * с готовым запросом: подключение — это разговор с агентом, а не отдельная форма.
 */
export function SourcesPanel({
  open,
  onOpenChange,
  nowMs,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  nowMs: number;
}) {
  const directory = useSpaceDirectory();
  return (
    <Sheet
      open={open}
      onOpenChange={onOpenChange}
      title="Источники"
      description="Вузы и то, откуда Ботай берёт данные. Подключения добавляются в чате."
      size="full"
      footer={
        <Button variant="primary" fullWidth leadingIcon="plus" asChild>
          <Link to="/chat" search={{ draft: addSourceDraft(null) }} onClick={() => onOpenChange(false)}>
            Добавить вуз
          </Link>
        </Button>
      }
    >
      {!directory.loaded ? (
        <SkeletonText lines={5} label="Загрузка источников" />
      ) : directory.spaces.length === 0 ? (
        <EmptyState
          icon="university"
          size="sm"
          title="Вузов пока нет"
          description="Напишите агенту, какой вуз подключить, — он найдёт сайт и проведёт через вход."
        />
      ) : (
        <div className="flex flex-col gap-6" data-sources-list="">
          {directory.spaces.map((space) => (
            <SpaceSources key={space.id} space={space} nowMs={nowMs} onNavigate={() => onOpenChange(false)} />
          ))}
        </div>
      )}
    </Sheet>
  );
}

/** Вуз и его источники: данные — тем же запросом v4, что и на «Учёбе». */
function SpaceSources({
  space,
  nowMs,
  onNavigate,
}: {
  space: SpaceInfo;
  nowMs: number;
  onNavigate: () => void;
}) {
  const { sources, loading, error } = useStudySources(space);
  return (
    <section className="flex flex-col gap-2" aria-labelledby={`source-space-${space.id}`}>
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
        <h3
          id={`source-space-${space.id}`}
          className="min-w-0 flex-1 basis-40 break-words type-subheading text-fg"
        >
          {space.title}
        </h3>
        <Button variant="ghost" size="sm" leadingIcon="plus" data-touch-expanded="" asChild>
          <Link
            to="/chat"
            search={{ draft: addSourceDraft(space) }}
            onClick={onNavigate}
            aria-label={`Добавить источник для ${space.shortTitle}`}
          >
            Добавить источник
          </Link>
        </Button>
      </div>
      {loading ? (
        <SkeletonText lines={3} label={`Загрузка источников: ${space.shortTitle}`} />
      ) : error ? (
        <p className="type-ui text-warning-fg">Источники загружаются: обновление идёт в фоне.</p>
      ) : sources.length === 0 ? (
        <p className="type-ui text-fg-muted">В этом вузе пока нет источников.</p>
      ) : (
        <ul className="flex flex-col divide-y divide-border rounded-lg border border-border">
          {sources.map((source) => (
            <SourceLine key={source.id} source={source} space={space} nowMs={nowMs} onNavigate={onNavigate} />
          ))}
        </ul>
      )}
    </section>
  );
}

function SourceLine({
  source,
  space,
  nowMs,
  onNavigate,
}: {
  source: StudySource;
  space: SpaceInfo;
  nowMs: number;
  onNavigate: () => void;
}) {
  const status = describeSourceStatus(source.status);
  const nature = describeSourceKind(source.kinds[0] ?? "other").label;
  const kinds = kindsLabel(source.kinds);
  const chat = sourceChatLinks(source, space.title);
  const target = source.threadId
    ? ({ to: "/chat", search: { thread: source.threadId } } as const)
    : status.needsUser || source.status === "error"
      ? (chat.repair ?? null)
      : null;
  const lastSync = source.lastSyncAt ? Date.parse(source.lastSyncAt) : Number.NaN;
  const body = (
    <>
      <Icon name={sourceIcon(source.kinds)} size={20} className="mt-1 shrink-0 text-fg-muted" />
      <span className="flex min-w-0 flex-1 flex-col gap-1">
        <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="min-w-0 break-words type-ui text-fg">{source.title}</span>
          <Badge tone={status.tone} icon={status.icon}>
            {status.label}
          </Badge>
        </span>
        <span className="type-caption text-fg-muted">
          {nature}
          {kinds && kinds !== nature ? ` · ${kinds}` : ""}
        </span>
        <span className="type-caption text-fg-subtle">
          {Number.isFinite(lastSync)
            ? `Обновлено ${formatAgo(lastSync, nowMs, space.timeZone)}`
            : "Ещё не обновлялся"}
        </span>
        {source.errorDetail && <span className="type-caption text-warning-fg">{source.errorDetail}</span>}
      </span>
      {target && <Icon name="chevron-right" size={16} className="mt-1 shrink-0 text-fg-subtle" />}
    </>
  );
  return (
    <li data-source-id={source.id} data-source-status={source.status}>
      {target ? (
        <Link
          to={target.to}
          search={target.search}
          onClick={onNavigate}
          className="flex min-h-14 items-start gap-3 px-3 py-3 transition-colors duration-fast ease-soft hover:bg-hover active:bg-pressed"
        >
          {body}
        </Link>
      ) : (
        <div className="flex min-h-14 items-start gap-3 px-3 py-3">{body}</div>
      )}
    </li>
  );
}
