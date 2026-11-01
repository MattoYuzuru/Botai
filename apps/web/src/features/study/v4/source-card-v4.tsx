import {
  Badge,
  Button,
  countRu,
  Dialog,
  displayHost,
  formatAgo,
  formatDay,
  formatTime,
  type IconName,
  ListRowBody,
  listRowClass,
  Sheet,
  SkeletonText,
  safeHttpUrl,
  toast,
} from "@botai/design";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { humanMessage } from "@/api/errors";
import {
  type ArtifactOut,
  deleteSource,
  disableSource,
  enableSource,
  isSourceBusyError,
  type PlaybookOut,
  type PlaybookVersionOut,
  priorityQuery,
  type RunReportOut,
  repairSource,
  setPrimarySource,
  sourceArtifactsQuery,
  sourceErrorOf,
  sourceKeys,
  sourcePlaybookQuery,
  sourcesQuery,
  sourceVersionsQuery,
  syncSource,
} from "@/api/sources";
import { PoliciesSection } from "@/features/actions/policies-section";
import { DetailList, DetailRow } from "@/features/data/detail-row";
import type { SpaceInfo } from "@/features/data/use-space-directory";
import { sourceChatLinks } from "./chat-links-v4";
import { SourceRunsSection } from "./source-runs";
import { dataKindsOf, describeSourceKind, describeSourceStatus, natureKindsOf } from "./status";
import { type StudySource, sourceFromApi } from "./use-study-sources";

export interface SourceCardV4Props {
  /** Выбранный источник; `null` — шит закрыт. Содержимое остаётся на время анимации закрытия. */
  source: StudySource | null;
  space: SpaceInfo | undefined;
  nowMs: number;
  onClose: () => void;
}

/**
 * Карточка источника (03-sources-and-ux.md §5.3, FR-065): что это, что читает, когда обновлялся,
 * описание чтения (playbook) с ревизиями, прочитанные артефакты, честная заглушка истории запусков и
 * действия: перечитать, починить, выключить/включить, сделать основным для расписания, открыть,
 * спросить агента. Секретов не показывает: их нет ни в v4-ответе, ни в старом подключении.
 */
export function SourceCardV4({ source, space, nowMs, onClose }: SourceCardV4Props) {
  return (
    <Sheet
      open={source !== null}
      onOpenChange={(open) => !open && onClose()}
      title={source?.title ?? "Источник"}
      description={
        space && source
          ? [space.shortTitle, describeSourceKind(source.kinds[0] ?? "other").label]
              .filter((part) => part !== source.title)
              .join(" · ")
          : undefined
      }
      size="half"
    >
      {source && space && (
        <SourceCardDetails key={source.id} source={source} space={space} nowMs={nowMs} onClose={onClose} />
      )}
    </Sheet>
  );
}

function SourceCardDetails({
  source,
  space,
  nowMs,
  onClose,
}: {
  source: StudySource;
  space: SpaceInfo;
  nowMs: number;
  onClose: () => void;
}) {
  // Действие меняет источник: карточка показывает ответ сервера, а не устаревшую копию из списка.
  const [state, setShown] = useState<StudySource>(source);
  const queryClient = useQueryClient();
  const isV4 = state.mode === "v4";
  // Список источников уже загружен панелью: после действия он обновляется, и карточка берёт оттуда
  // свежие статус, время синхронизации и расписание (а не запомненную копию на момент открытия).
  const sourceList = useQuery({ ...sourcesQuery(space.id), enabled: isV4 });
  const fresh = sourceList.data?.find((row) => row.id === state.id);
  const shown: StudySource = fresh
    ? {
        ...state,
        status: fresh.status,
        lastSyncAt: fresh.lastSyncAt,
        nextSyncAt: fresh.nextSyncAt ?? null,
        errorCode: sourceErrorOf(fresh).code,
        errorDetail: sourceErrorOf(fresh).detail,
        threadId: fresh.threadId ?? null,
        playbookId: fresh.playbookId,
      }
    : state;
  const status = describeSourceStatus(shown.status);
  const chat = sourceChatLinks(shown, space.title);
  const open = safeHttpUrl(shown.entryUrl) ?? safeHttpUrl(shown.origin);
  const openUrl = open?.href ?? null;
  const lastSync = shown.lastSyncAt ? Date.parse(shown.lastSyncAt) : Number.NaN;
  const lastSyncText = Number.isFinite(lastSync)
    ? `${formatDay(lastSync, nowMs, space.timeZone)}, ${formatTime(lastSync, space.timeZone)}`
    : "Ещё не обновлялся";
  const natures = natureKindsOf(shown.kinds);
  const dataKinds = dataKindsOf(shown.kinds);
  const schedule = dataKinds.includes("schedule");
  const liveStatus = !status.disabled && shown.status !== "archived";
  const nextSync = shown.nextSyncAt ? Date.parse(shown.nextSyncAt) : Number.NaN;
  const nextSyncText = Number.isFinite(nextSync)
    ? `${formatDay(nextSync, nowMs, space.timeZone)}, ${formatTime(nextSync, space.timeZone)}`
    : "Расписание не задано";

  const playbook = useQuery({ ...sourcePlaybookQuery(shown.id), enabled: isV4 });
  const versions = useQuery({
    ...sourceVersionsQuery(shown.id),
    enabled: isV4 && Boolean(playbook.data),
  });
  const artifacts = useQuery({ ...sourceArtifactsQuery(shown.id), enabled: isV4 });
  const priority = useQuery({ ...priorityQuery(space.id, "schedule"), enabled: isV4 && schedule });
  const primaryScheduleId = priority.data?.sourceIds[0] ?? null;
  const isPrimarySchedule = primaryScheduleId === shown.id;

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: sourceKeys.list(space.id) });
    void queryClient.invalidateQueries({ queryKey: sourceKeys.playbook(shown.id) });
    void queryClient.invalidateQueries({ queryKey: sourceKeys.versions(shown.id) });
    void queryClient.invalidateQueries({ queryKey: sourceKeys.artifacts(shown.id) });
    void queryClient.invalidateQueries({ queryKey: sourceKeys.runs(shown.id) });
    void queryClient.invalidateQueries({ queryKey: sourceKeys.priority(space.id, "schedule") });
  };

  const sync = useMutation({
    mutationFn: () => syncSource(shown.id),
    onSuccess: (report) => {
      applyReport(report, "перечитан");
      invalidate();
    },
    onError: (error) => failed("Не удалось перечитать источник", error),
  });
  const repair = useMutation({
    mutationFn: () => repairSource(shown.id),
    onSuccess: (report) => {
      applyReport(report, "обновлён после починки");
      invalidate();
    },
    onError: (error) => failed("Не удалось починить источник", error),
  });
  const disable = useMutation({
    mutationFn: () => disableSource(shown.id),
    onSuccess: (updated) => {
      setShown(sourceFromApi(updated));
      invalidate();
      toast("Источник выключен", {
        tone: "success",
        description: "Данные скрыты из разделов, задачи остановлены. Включить можно в этой карточке.",
      });
    },
    onError: (error) => failed("Не удалось выключить источник", error),
  });
  const enable = useMutation({
    mutationFn: () => enableSource(shown.id),
    onSuccess: (updated) => {
      setShown(sourceFromApi(updated));
      invalidate();
      toast("Источник включён", { tone: "success", description: "Обновление пойдёт в фоне." });
    },
    onError: (error) => failed("Не удалось включить источник", error),
  });
  const makePrimary = useMutation({
    mutationFn: () =>
      setPrimarySource(space.id, "schedule", [
        shown.id,
        ...(priority.data?.sourceIds ?? []).filter((id) => id !== shown.id),
      ]),
    onSuccess: () => {
      invalidate();
      toast(`Основное расписание — «${shown.title}»`, {
        tone: "success",
        description: "Что изменится, решает размеренный порядок источников; данные не удаляются.",
      });
    },
    onError: (error) => failed("Не удалось выбрать основной источник", error),
  });
  const remove = useMutation({
    mutationFn: () => deleteSource(shown.id),
    onSuccess: () => {
      invalidate();
      toast("Источник удалён", {
        tone: "success",
        description: "Данные источника больше не показываются в разделах.",
      });
      onClose();
    },
    onError: (error) => {
      failed(isSourceBusyError(error) ? "Источник сейчас обновляется" : "Не удалось удалить источник", error);
    },
  });
  const [confirmDisable, setConfirmDisable] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  function applyReport(report: RunReportOut, pastTense: string) {
    const records = Object.values(report.counts).reduce((sum, value) => sum + value, 0);
    const next = report.sourceStatus ?? shown.status;
    setShown((current) => ({
      ...current,
      status: next,
      lastSyncAt: report.ok ? new Date().toISOString() : current.lastSyncAt,
      errorDetail: report.ok ? null : (report.errors[0]?.detail ?? current.errorDetail),
      errorCode: report.ok ? null : (report.errors[0]?.code ?? current.errorCode),
    }));
    if (report.ok) {
      toast(`Источник ${pastTense}`, {
        tone: "success",
        description:
          records > 0
            ? `${countRu(records, ["запись", "записи", "записей"])} получено.`
            : "Сервер не назвал число записей — покажем их в разделах.",
      });
    } else {
      toast(`Источник не ${pastTense.split(" ")[0]}`, {
        tone: "error",
        description: report.errors[0]?.detail ?? "Попробуем ещё раз в фоне.",
      });
    }
  }

  return (
    <div className="flex flex-col gap-4 pb-2">
      <div className="flex flex-wrap gap-1.5">
        <Badge tone={status.tone} icon={status.icon}>
          {status.label}
        </Badge>
        {isPrimarySchedule && <Badge tone="accent">Основное расписание</Badge>}
        {shown.trust && shown.trust !== "normal" && (
          <Badge tone="neutral">{shown.trust === "high" ? "Доверенный" : "Низкое доверие"}</Badge>
        )}
      </div>
      <p className="type-ui text-fg-muted">{status.detail}</p>

      <DetailList label={`Источник: ${shown.title}`}>
        <DetailRow icon="source" label="Что это">
          {natures.length > 0
            ? natures.map((kind) => describeSourceKind(kind).label).join(", ")
            : "Вид ещё определяется"}
        </DetailRow>
        <DetailRow icon="document" label="Что читает">
          {dataKinds.length > 0
            ? dataKinds.map((kind) => (
                <Badge key={kind} tone="neutral" icon={describeSourceKind(kind).icon} className="mr-1 mb-1">
                  {describeSourceKind(kind).label}
                </Badge>
              ))
            : "Данных пока нет: агент ещё не прочитал источник"}
        </DetailRow>
        <DetailRow icon="time" label="Последняя синхронизация">
          {lastSyncText}
        </DetailRow>
        {isV4 && (
          <DetailRow icon="sync" label="Следующее обновление">
            {nextSyncText}
          </DetailRow>
        )}
        {shown.status === "disabled" && (
          <DetailRow icon="paused" label="Состояние">
            Выключен владельцем: задач нет, данные скрыты. Включение вернёт источник в работу.
          </DetailRow>
        )}
        {openUrl && (
          <DetailRow icon="site" label="Страница источника">
            <span className="break-all">{displayHost(open as URL)}</span>
          </DetailRow>
        )}
        {shown.errorDetail && (
          <DetailRow icon="error" label="Что не получается">
            <span className="text-warning-fg">{shown.errorDetail}</span>
          </DetailRow>
        )}
      </DetailList>

      <PlaybookSection
        mode={shown.mode}
        loading={playbook.isPending}
        playbook={playbook.data ?? null}
        versions={versions.data ?? []}
        versionsLoading={versions.isPending && Boolean(playbook.data)}
        nowMs={nowMs}
        timeZone={space.timeZone}
      />

      <ArtifactsSection
        mode={shown.mode}
        artifacts={artifacts.data ?? []}
        loading={artifacts.isPending}
        error={Boolean(artifacts.error)}
        nowMs={nowMs}
        timeZone={space.timeZone}
      />

      <PoliciesSection
        scopeKind="source"
        scopeId={shown.id}
        sourceId={shown.id}
        spaceId={space.id}
        journalSourceId={shown.id}
      />

      <SourceRunsSection source={shown} nowMs={nowMs} timeZone={space.timeZone} />

      <div className="flex flex-col gap-2">
        {openUrl && (
          <Button variant="secondary" leadingIcon="external" asChild fullWidth>
            <a
              href={openUrl}
              target="_blank"
              rel="noopener noreferrer"
              onClick={onClose}
              aria-label={`Открыть сайт: ${shown.title}`}
            >
              Открыть
            </a>
          </Button>
        )}
        {(shown.status === "active" || shown.status === "stale") && (
          <Button
            variant="secondary"
            leadingIcon="sync"
            fullWidth
            loading={sync.isPending}
            onClick={() => sync.mutate()}
          >
            Перечитать
          </Button>
        )}
        {shown.status === "error" && (
          <Button
            variant="secondary"
            leadingIcon="repairing"
            fullWidth
            loading={repair.isPending}
            onClick={() => repair.mutate()}
          >
            Починить
          </Button>
        )}
        {schedule && isV4 && !isPrimarySchedule && (
          <Button
            variant="secondary"
            leadingIcon="check"
            fullWidth
            loading={makePrimary.isPending}
            onClick={() => makePrimary.mutate()}
            aria-label={`Сделать основным для расписания: ${shown.title}`}
          >
            Сделать основным для расписания
          </Button>
        )}
        {liveStatus && (
          <Button
            variant="secondary"
            fullWidth
            onClick={() => setConfirmDisable(true)}
            loading={disable.isPending}
          >
            Выключить источник
          </Button>
        )}
        {shown.status === "disabled" && (
          <Button
            variant="primary"
            leadingIcon="ok"
            fullWidth
            loading={enable.isPending}
            onClick={() => enable.mutate()}
          >
            Включить
          </Button>
        )}
        <Button
          variant="ghost"
          tone="danger"
          fullWidth
          onClick={() => setConfirmDelete(true)}
          loading={remove.isPending}
        >
          Удалить источник
        </Button>
        {chat.openThread && (
          <Button variant="ghost" leadingIcon="chat" asChild fullWidth>
            <Link {...chat.openThread} onClick={onClose}>
              Открыть тред задачи
            </Link>
          </Button>
        )}
        {shown.status === "error" && (
          <Button variant="ghost" leadingIcon="chat" asChild fullWidth>
            <Link {...chat.ask} onClick={onClose} aria-label={`Спросить агента: ${shown.title}`}>
              Спросить агента
            </Link>
          </Button>
        )}
        {shown.status !== "error" && chat.repair && (
          <Button variant="primary" leadingIcon="chat" asChild fullWidth>
            <Link {...chat.repair} onClick={onClose} aria-label={`${chat.repairLabel}: ${shown.title}`}>
              {chat.repairLabel}
            </Link>
          </Button>
        )}
        {shown.status !== "error" && shown.status !== "disabled" && !chat.repair && (
          <Button variant="primary" leadingIcon="chat" asChild fullWidth>
            <Link {...chat.ask} onClick={onClose} aria-label={`Спросить агента: ${shown.title}`}>
              Спросить агента
            </Link>
          </Button>
        )}
        {shown.status === "disabled" && (
          <Button variant="ghost" leadingIcon="chat" asChild fullWidth>
            <Link {...chat.ask} onClick={onClose} aria-label={`Спросить агента: ${shown.title}`}>
              Спросить агента
            </Link>
          </Button>
        )}
      </div>

      <Dialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={`Удалить «${shown.title}»?`}
        description="Данные источника исчезнут из «Учёбы» и разделов: расписание, сроки, задания, оценки и документы, прочитанные из него. Если нужен перерыв, используйте «Выключить источник» — это обратимо."
        confirmLabel="Удалить источник"
        tone="danger"
        onConfirm={async () => {
          await remove.mutateAsync().catch(() => undefined);
        }}
      />
      <Dialog
        open={confirmDisable}
        onOpenChange={setConfirmDisable}
        title={`Выключить «${shown.title}»?`}
        description="Данные источника скроются из разделов, задачи остановятся. Ничего не удаляется: источник можно включить обратно."
        confirmLabel="Выключить источник"
        tone="default"
        onConfirm={async () => {
          await disable.mutateAsync().catch(() => undefined);
        }}
      />
    </div>
  );
}

function failed(title: string, error: unknown) {
  toast(title, { tone: "error", description: humanMessage(error) });
}

const PLAYBOOK_FORMAT: Record<string, string> = {
  declarative: "Декларативное описание",
  recipe: "Рецепт",
  python: "Код на Python",
  js: "Код на JavaScript",
};

function PlaybookSection({
  mode,
  loading,
  playbook,
  versions,
  versionsLoading,
  nowMs,
  timeZone,
}: {
  mode: StudySource["mode"];
  loading: boolean;
  playbook: PlaybookOut | null;
  versions: readonly PlaybookVersionOut[];
  versionsLoading: boolean;
  nowMs: number;
  timeZone: string;
}) {
  return (
    <section aria-labelledby="source-playbook-title" className="flex flex-col gap-2">
      <h3 id="source-playbook-title" className="type-subheading text-fg">
        Как читает
      </h3>
      {mode !== "v4" || loading ? (
        <SkeletonText lines={2} label="Загрузка описания чтения" />
      ) : playbook === null ? (
        <p className="type-ui text-fg-muted">
          Описание чтения ещё не создано: агент сделает его при подключении источника.
        </p>
      ) : (
        <>
          <DetailList label="Описание чтения">
            <DetailRow icon="repairing" label="Формат">
              {PLAYBOOK_FORMAT[playbook.language] ?? playbook.language}
            </DetailRow>
            <DetailRow icon="history" label="Ревизия">
              {playbook.currentVersion}
            </DetailRow>
            <DetailRow icon="ok" label="Проверка">
              {playbook.health?.ok === false
                ? "Последняя проверка не удалась — агент починит описание."
                : "Последняя проверка прошла."}
            </DetailRow>
          </DetailList>
          {versionsLoading ? (
            <SkeletonText lines={2} label="Загрузка ревизий" />
          ) : (
            versions.length > 0 && (
              <ul aria-label="Ревизии описания чтения">
                {versions
                  .slice()
                  .sort((a, b) => b.version - a.version)
                  .slice(0, 3)
                  .map((version) => (
                    <li key={version.id}>
                      <div className={listRowClass}>
                        <ListRowBody
                          icon="history"
                          title={`Ревизия ${version.version}`}
                          meta={
                            <>
                              <Badge tone={version.active ? "success" : "neutral"}>
                                {version.active
                                  ? "Активна"
                                  : version.canary?.ok === true
                                    ? "Проверена"
                                    : "Черновик"}
                              </Badge>
                              <time dateTime={version.createdAt}>
                                {formatAgo(Date.parse(version.createdAt), nowMs, timeZone)}
                              </time>
                            </>
                          }
                        />
                      </div>
                    </li>
                  ))}
              </ul>
            )
          )}
        </>
      )}
    </section>
  );
}

const ARTIFACT_KIND: Record<string, { label: string; icon: IconName }> = {
  page: { label: "Страница", icon: "site" },
  json: { label: "Данные", icon: "connection" },
  file: { label: "Файл", icon: "file" },
  doc: { label: "Документ", icon: "document" },
  har: { label: "Запись сети", icon: "download" },
  screenshot: { label: "Снимок экрана", icon: "image" },
  log: { label: "Журнал", icon: "document" },
  other: { label: "Материал", icon: "info" },
};

const FALLBACK_ARTIFACT: { label: string; icon: IconName } = { label: "Материал", icon: "info" };

const artifactView = (kind: string): { label: string; icon: IconName } =>
  ARTIFACT_KIND[kind] ?? FALLBACK_ARTIFACT;

function artifactTitle(artifact: ArtifactOut): string {
  const title = artifact.metadata?.title;
  if (typeof title === "string" && title.trim()) return title;
  const url = safeHttpUrl(artifact.url);
  if (url) return displayHost(url);
  return artifactView(artifact.kind).label;
}

function ArtifactsSection({
  mode,
  artifacts,
  loading,
  error,
  nowMs,
  timeZone,
}: {
  mode: StudySource["mode"];
  artifacts: readonly ArtifactOut[];
  loading: boolean;
  error: boolean;
  nowMs: number;
  timeZone: string;
}) {
  if (mode !== "v4") return null;
  return (
    <section aria-labelledby="source-artifacts-title" className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <h3 id="source-artifacts-title" className="type-subheading text-fg">
          Прочитанное
        </h3>
        {artifacts.length > 0 && <span className="type-caption text-fg-muted">{artifacts.length}</span>}
      </div>
      {loading ? (
        <SkeletonText lines={2} label="Загрузка прочитанного" />
      ) : error ? (
        <p className="type-ui text-fg-muted">Прочитанное не загрузилось: обновление идёт в фоне.</p>
      ) : artifacts.length === 0 ? (
        <p className="type-ui text-fg-muted">Пока ничего не прочитано.</p>
      ) : (
        <ul aria-label="Прочитанное">
          {artifacts.slice(0, 5).map((artifact) => {
            const view = artifactView(artifact.kind);
            return (
              <li key={artifact.id}>
                <div className={listRowClass}>
                  <ListRowBody
                    icon={view.icon}
                    title={artifactTitle(artifact)}
                    meta={
                      <>
                        <span>{view.label}</span>
                        <time dateTime={artifact.capturedAt}>
                          {formatAgo(Date.parse(artifact.capturedAt), nowMs, timeZone)}
                        </time>
                      </>
                    }
                  />
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
