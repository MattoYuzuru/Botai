import {
  Badge,
  Banner,
  Button,
  Card,
  DEFAULT_TIME_ZONE,
  EmptyState,
  formatBytes,
  formatDay,
  formatTime,
  formatUpdated,
  Icon,
  type IconName,
  ListRowBody,
  listRowClass,
  ProgressRail,
  ScrollArea,
  Skeleton,
  SkeletonText,
  useNow,
} from "@botai/design";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useNavigate, useRouter } from "@tanstack/react-router";
import { type ReactNode, useState } from "react";
import {
  type AgentRun,
  type AgentRunEvent,
  agentRunEventsQuery,
  agentRunQuery,
  isAgentRunsUnavailable,
  isRunning,
} from "@/api/agent-runs";
import { humanMessage } from "@/api/errors";
import { Screen } from "@/app/screen";
import {
  argumentRows,
  artifactLabel,
  artifactsFromEvents,
  asInt,
  asNumber,
  asRecord,
  asText,
  describeEventKind,
  describeRunStatus,
  eventCaption,
  eventError,
  eventHeadline,
  eventSummary,
  formatCostUsd,
  formatRunDuration,
  formatTokens,
  limitKeyLabel,
  limitLabel,
  modelContent,
  purposeLabel,
  type RunTone,
  runDurationMs,
  toolResultText,
} from "./trace";

const TIME_ZONE = DEFAULT_TIME_ZONE;

/**
 * Трасса хода агента v4 (D-014, docs/v4/02-architecture.md §4.8): состояние, шаги с вызовами
 * инструментов и их результатами, ошибки, токены и стоимость, артефакты, лимиты и пауза.
 * Владелец видит свой ход целиком; значения уже маскированы сервером, клиент ничего не исполняет.
 */
export function AgentRunTraceScreen({ runId }: { runId: string }) {
  const router = useRouter();
  const navigate = useNavigate();
  const run = useQuery(agentRunQuery(runId));
  const running = isRunning(run.data);
  const nowMs = useNow(1_000);
  const events = useInfiniteQuery({
    ...agentRunEventsQuery(runId, running),
    enabled: run.data !== undefined,
  });
  const back = () => {
    if (router.history.canGoBack()) router.history.back();
    else void navigate({ to: "/chat" });
  };

  if (run.isPending) {
    return (
      <Screen title="Трасса агента" leading="back" onBack={back}>
        <div
          data-screen="agent-run"
          data-state="loading"
          role="status"
          aria-busy="true"
          className="flex flex-col gap-3"
        >
          <span className="sr-only">Загрузка трассы</span>
          <Skeleton width="50%" className="h-6" />
          <Skeleton className="h-28 rounded-lg" />
          <Skeleton className="h-16 rounded-lg" />
          <Skeleton className="h-16 rounded-lg" />
        </div>
      </Screen>
    );
  }

  if (run.isError) {
    const unavailable = isAgentRunsUnavailable(run.error);
    return (
      <Screen title="Трасса агента" leading="back" onBack={back}>
        <div data-screen="agent-run" data-state={unavailable ? "empty" : "error"}>
          {unavailable ? (
            <EmptyState
              icon="history"
              title="Трассы на этом сервере нет"
              description="Ходы агента v4 выключены на этом сервере или такой трассы нет у вашей учётной записи."
              size="sm"
            />
          ) : (
            <Banner tone="warning" icon="offline" live>
              Трасса не загрузилась: {humanMessage(run.error)} Обновление идёт в фоне.
            </Banner>
          )}
        </div>
      </Screen>
    );
  }

  const data = run.data;
  if (!data) return null;
  const details = events.data?.pages.flatMap((page) => page.events) ?? [];
  const unique = [...new Map(details.map((event) => [event.seq, event])).values()].sort(
    (left, right) => left.seq - right.seq,
  );
  const artifacts = artifactsFromEvents(unique);
  const lastLimit = unique.findLast((event) => event.kind === "limit");
  const lastError = unique.findLast((event) => event.kind === "error");

  return (
    <Screen title="Трасса агента" leading="back" onBack={back}>
      <div data-screen="agent-run" data-state="ok" className="flex flex-col gap-6">
        <RunSummary run={data} nowMs={nowMs} />
        <RunStateNote
          status={data.status}
          limitEvent={isLimitStatus(data.status) ? lastLimit : undefined}
          errorEvent={data.status === "error" ? lastError : undefined}
        />
        {running && (
          <Card padding="sm" data-part="run-live" className="px-4 py-3">
            <ProgressRail
              stage={data.steps}
              stageCount={null}
              elapsedMs={runDurationMs(data, nowMs) ?? 0}
              etaKnown={false}
            />
          </Card>
        )}
        {artifacts.length > 0 && <ArtifactsSection artifacts={artifacts} />}
        <StepsSection
          events={unique}
          loading={events.isPending}
          error={events.isError && !isAgentRunsUnavailable(events.error) ? events.error : null}
          hasMore={Boolean(events.hasNextPage)}
          loadingMore={events.isFetchingNextPage}
          onMore={() => void events.fetchNextPage()}
          running={running}
        />
      </div>
    </Screen>
  );
}

function isLimitStatus(status: string): boolean {
  return (
    status === "step_limit" ||
    status === "time_limit" ||
    status === "token_limit" ||
    status === "cost_limit" ||
    status === "hard_stop"
  );
}

function RunSummary({ run, nowMs }: { run: AgentRun; nowMs: number }) {
  const status = describeRunStatus(run.status);
  const started = run.startedAt ? Date.parse(run.startedAt) : null;
  const finished = run.finishedAt ? Date.parse(run.finishedAt) : null;
  const duration = runDurationMs(run, nowMs);
  return (
    <section aria-labelledby="run-summary-title" className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <h2 id="run-summary-title" className="type-heading text-fg">
          Ход агента
        </h2>
        <Badge tone={status.tone} icon={status.icon}>
          {status.label}
        </Badge>
        {run.steps > 0 && <Badge tone="neutral">{`шагов: ${run.steps}`}</Badge>}
      </div>
      <p className="type-ui text-fg-muted">{status.detail}</p>
      <Card padding="sm" data-part="run-task" className="flex flex-col gap-2 px-4 pt-3 pb-3">
        <h3 className="type-subheading text-fg">Задача</h3>
        <p className="whitespace-pre-line break-words type-body text-fg">
          {run.task || "Задача не записана."}
        </p>
        <div className="flex flex-wrap gap-x-4 gap-y-1 type-caption text-fg-muted">
          <span>{run.provider || "провайдер не указан"}</span>
          <span>{run.model || "модель не указана"}</span>
          {run.jobId && <span className="break-all">задание: {run.jobId}</span>}
        </div>
      </Card>
      <dl
        aria-label="Сводка хода"
        className="grid grid-cols-1 gap-x-4 gap-y-2 type-caption text-fg-muted xs:grid-cols-2 lg:grid-cols-4"
      >
        <SummaryItem label="Начало">
          {started !== null
            ? `${formatDay(started, nowMs, TIME_ZONE)}, ${formatTime(started, TIME_ZONE)}`
            : "не начался"}
        </SummaryItem>
        <SummaryItem label="Длительность">
          {duration !== null ? formatRunDuration(duration) : "неизвестно"}
        </SummaryItem>
        <SummaryItem label="Токены">{formatTokens(run.totalTokens)}</SummaryItem>
        <SummaryItem label="Стоимость">{formatCostUsd(run.costUsd)}</SummaryItem>
        <SummaryItem label="Шаги">{run.steps}</SummaryItem>
        <SummaryItem label="События">{run.eventCount}</SummaryItem>
        <SummaryItem label="Завершён">
          {finished !== null ? formatUpdated(finished, nowMs, TIME_ZONE) : "ещё идёт"}
        </SummaryItem>
      </dl>
    </section>
  );
}

function SummaryItem({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col">
      <dt>{label}</dt>
      <dd className="type-ui text-fg" data-numeric="">
        {children}
      </dd>
    </div>
  );
}

function RunStateNote({
  status,
  limitEvent,
  errorEvent,
}: {
  status: string;
  limitEvent: AgentRunEvent | undefined;
  errorEvent: AgentRunEvent | undefined;
}) {
  if (status === "paused") {
    return (
      <div data-part="run-paused">
        <Banner tone="warning" icon="paused" live>
          Ход на паузе: агент остановлен и ждёт продолжения.
        </Banner>
      </div>
    );
  }
  if (status === "cancelled") {
    return (
      <div data-part="run-cancelled">
        <Banner tone="neutral" icon="paused">
          Ход отменён. Сделанное сохранено в трассе.
        </Banner>
      </div>
    );
  }
  if (isLimitStatus(status)) {
    const payload = limitEvent ? (asRecord(limitEvent.payload) ?? {}) : {};
    const numbers: string[] = [];
    const steps = asInt(payload.steps);
    const tokens = asInt(payload.total_tokens);
    const cost = asNumber(payload.cost_usd);
    if (steps !== null) numbers.push(`шагов: ${steps}`);
    if (tokens !== null) numbers.push(formatTokens(tokens));
    if (cost !== null) numbers.push(formatCostUsd(cost));
    return (
      <div data-part="run-limit">
        <Banner tone={status === "hard_stop" ? "danger" : "warning"} icon="warning" live>
          <span className="block">
            Ход остановлен: {limitLabel(payload.limit ?? limitFromStatus(status))}. Сделанное сохранено в
            трассе.
          </span>
          {numbers.length > 0 && <span className="block type-caption">{numbers.join(" · ")}</span>}
        </Banner>
      </div>
    );
  }
  if (status === "error") {
    const error = errorEvent ? eventError(errorEvent) : { code: null, detail: null };
    return (
      <div data-part="run-error">
        <Banner tone="danger" icon="error" live>
          <span className="block">{error.detail ?? "Ход завершился ошибкой."}</span>
          {error.code && <span className="block type-caption">код: {error.code}</span>}
        </Banner>
      </div>
    );
  }
  return null;
}

function limitFromStatus(status: string): string {
  switch (status) {
    case "step_limit":
      return "steps";
    case "time_limit":
      return "seconds";
    case "token_limit":
      return "tokens";
    case "cost_limit":
      return "usd";
    default:
      return "hard_stop_usd";
  }
}

function ArtifactsSection({ artifacts }: { artifacts: ReturnType<typeof artifactsFromEvents> }) {
  return (
    <section aria-labelledby="run-artifacts-title" className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <h2 id="run-artifacts-title" className="type-subheading text-fg">
          Артефакты
        </h2>
        <span className="type-caption text-fg-muted">{artifacts.length}</span>
      </div>
      <Card padding="sm">
        <ul aria-label="Артефакты хода">
          {artifacts.map((artifact) => (
            <li key={artifact.id}>
              <div className={listRowClass}>
                <ListRowBody
                  icon={artifactIcon(artifact.kind)}
                  title={artifactLabel(artifact.kind)}
                  meta={
                    <>
                      <span className="break-all">{artifact.id}</span>
                      {artifact.bytes !== null && <span>{formatBytes(artifact.bytes)}</span>}
                    </>
                  }
                />
              </div>
            </li>
          ))}
        </ul>
      </Card>
    </section>
  );
}

function artifactIcon(kind: string): IconName {
  if (kind === "screenshot" || kind === "image") return "image";
  if (kind === "har" || kind === "download") return "download";
  if (kind === "page" || kind === "doc") return "document";
  return "attach";
}

function StepsSection({
  events,
  loading,
  error,
  hasMore,
  loadingMore,
  onMore,
  running,
}: {
  events: AgentRunEvent[];
  loading: boolean;
  error: unknown;
  hasMore: boolean;
  loadingMore: boolean;
  onMore: () => void;
  running: boolean;
}) {
  return (
    <section aria-labelledby="run-steps-title" className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-2">
        <h2 id="run-steps-title" className="type-heading text-fg">
          Шаги
        </h2>
        {events.length > 0 && <span className="type-caption text-fg-muted">{events.length}</span>}
      </div>
      {loading ? (
        <SkeletonText lines={4} label="Загрузка шагов трассы" />
      ) : error ? (
        <Banner tone="warning" icon="offline" live>
          Шаги не загрузились: {humanMessage(error)} Обновление идёт в фоне.
        </Banner>
      ) : events.length === 0 ? (
        <p className="type-ui text-fg-muted">
          {running
            ? "Событий пока нет: шаги появятся сами, как только агент начнёт."
            : "Событий в трассе нет."}
        </p>
      ) : (
        <ol aria-label="Шаги трассы" className="flex flex-col gap-2">
          {events.map((event) => (
            <li key={event.seq}>
              <EventCard event={event} />
            </li>
          ))}
        </ol>
      )}
      {hasMore && (
        <Button variant="secondary" fullWidth loading={loadingMore} onClick={onMore}>
          Показать ещё
        </Button>
      )}
      {running && !hasMore && events.length > 0 && (
        <p className="type-caption text-fg-subtle">Новые шаги появятся здесь сами.</p>
      )}
    </section>
  );
}

function EventCard({ event }: { event: AgentRunEvent }) {
  const kind = describeEventKind(event.kind);
  const [open, setOpen] = useState(false);
  const tone = eventTone(event.kind, event);
  const payload = asRecord(event.payload) ?? {};
  const summary = eventSummary(event);
  const rows = event.kind === "tool_call" ? argumentRows(payload.arguments) : [];
  const content = event.kind === "model_call" ? modelContent(event) : null;
  const result = event.kind === "tool_result" ? toolResultText(event) : null;
  const error = event.kind === "error" ? eventError(event) : null;
  const hasDetails =
    rows.length > 0 || Boolean(content) || Boolean(result) || Boolean(error?.code) || Boolean(error?.detail);

  return (
    <Card
      padding="sm"
      data-event-kind={event.kind}
      data-event-seq={event.seq}
      className="flex flex-col gap-2"
    >
      <div className="flex items-start gap-2 px-1 pt-1">
        <Icon name={kind.icon} size={20} className={`mt-1 ${ICON_TONE[tone]}`} />
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <p className="min-w-0 break-words type-subheading text-fg">{eventHeadline(event)}</p>
          <p className="type-caption text-fg-muted">{eventCaption(event, TIME_ZONE)}</p>
        </div>
        {event.kind === "tool_result" && (
          <Badge tone={payload.ok === false ? "danger" : "success"}>
            {payload.ok === false ? "Не выполнено" : "Выполнено"}
          </Badge>
        )}
      </div>
      {summary && <p className="whitespace-pre-line break-words px-1 type-ui text-fg-muted">{summary}</p>}
      {event.kind === "model_call" && <ModelStats payload={payload} />}
      {event.kind === "run_started" && <RunStartedFacts payload={payload} />}
      {event.kind === "limit" && <LimitFacts payload={payload} />}
      {hasDetails && (
        <div className="flex flex-col gap-2 px-1 pb-1">
          <Button
            variant="ghost"
            size="sm"
            trailingIcon={open ? "chevron-up" : "chevron-down"}
            aria-expanded={open}
            onClick={() => setOpen((value) => !value)}
            className="self-start"
            data-touch-expanded=""
          >
            {open ? "Свернуть подробности" : "Показать подробности"}
          </Button>
          {open && (
            <div className="flex flex-col gap-3" data-event-details="">
              {rows.length > 0 && (
                <dl aria-label="Аргументы вызова" className="flex flex-col gap-1">
                  {rows.map((row) => (
                    <div key={row.name} className="flex flex-col gap-1">
                      <dt className="type-caption text-fg-muted">{row.name}</dt>
                      <dd className="whitespace-pre-wrap break-all type-mono text-fg">{row.value}</dd>
                    </div>
                  ))}
                </dl>
              )}
              {content && <TraceText label="Текст модели" text={content} />}
              {result && <TraceText label="Результат" text={result} />}
              {error && (error.detail || error.code) && (
                <div className="flex flex-col gap-1">
                  {error.detail && <p className="break-words type-ui text-danger-fg">{error.detail}</p>}
                  {error.code && <p className="type-caption text-fg-subtle">код: {error.code}</p>}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

function TraceText({ label, text }: { label: string; text: string }) {
  return (
    <div className="flex flex-col gap-1">
      <p className="type-caption text-fg-muted">{label}</p>
      <ScrollArea className="max-h-96 rounded-md bg-bg-subtle" viewportClassName="p-3">
        <pre className="whitespace-pre-wrap break-all type-mono text-fg">{text}</pre>
      </ScrollArea>
    </div>
  );
}

function ModelStats({ payload }: { payload: Record<string, unknown> }) {
  const parts: string[] = [];
  const hit = asInt(payload.input_cache_hit);
  const miss = asInt(payload.input_cache_miss);
  const output = asInt(payload.output);
  const reasoning = asInt(payload.reasoning);
  const cost = asNumber(payload.cost_usd);
  if (miss !== null || output !== null) {
    parts.push(`вход: ${(hit ?? 0) + (miss ?? 0)}`);
  }
  if (output !== null) parts.push(`ответ: ${output}`);
  if (reasoning) parts.push(`рассуждение: ${reasoning}`);
  if (payload.reasoning_present === true) parts.push("есть рассуждение");
  if (cost !== null) parts.push(formatCostUsd(cost));
  return parts.length > 0 ? (
    <p className="flex flex-wrap gap-x-3 gap-y-1 px-1 type-caption text-fg-muted" data-part="model-stats">
      {parts.map((part) => (
        <span key={part}>{part}</span>
      ))}
    </p>
  ) : null;
}

function RunStartedFacts({ payload }: { payload: Record<string, unknown> }) {
  const limits = asRecord(payload.limits) ?? {};
  const toolCount = asInt(payload.tool_count);
  const purpose = asText(payload.purpose);
  const chips: string[] = [];
  if (toolCount !== null) chips.push(`инструментов: ${toolCount}`);
  if (purpose) chips.push(`назначение: ${purposeLabel(purpose)}`);
  const limitText = Object.entries(limits)
    .map(([name, value]) => {
      if (typeof value === "number") return `${limitKeyLabel(name)}: ${value}`;
      return null;
    })
    .filter((row): row is string => row !== null);
  return (
    <div className="flex flex-col gap-1 px-1">
      {chips.length > 0 && <p className="type-caption text-fg-muted">{chips.join(" · ")}</p>}
      {limitText.length > 0 && (
        <p className="type-caption text-fg-subtle">лимиты — {limitText.join(" · ")}</p>
      )}
    </div>
  );
}

function LimitFacts({ payload }: { payload: Record<string, unknown> }) {
  const parts: string[] = [];
  const steps = asInt(payload.steps);
  const elapsed = asNumber(payload.elapsed_s);
  const tokens = asInt(payload.total_tokens);
  const cost = asNumber(payload.cost_usd);
  if (steps !== null) parts.push(`шагов: ${steps}`);
  if (elapsed !== null) parts.push(`время: ${formatRunDuration(elapsed * 1000)}`);
  if (tokens !== null) parts.push(formatTokens(tokens));
  if (cost !== null) parts.push(formatCostUsd(cost));
  const auto = payload.auto_continue === true;
  return parts.length > 0 ? (
    <p className="px-1 type-caption text-fg-muted">
      {auto ? "Мягкий лимит — ход продолжается автоматически. " : ""}
      {parts.join(" · ")}
    </p>
  ) : null;
}

const ICON_TONE: Record<RunTone, string> = {
  success: "text-success-fg",
  info: "text-info-fg",
  warning: "text-warning-fg",
  danger: "text-danger-fg",
  neutral: "text-fg-subtle",
};

function eventTone(kind: string, event: AgentRunEvent): RunTone {
  if (kind === "error") return "danger";
  if (kind === "limit") return "warning";
  if (kind === "pause" || kind === "cancel") return "warning";
  if (kind === "tool_result") {
    const payload = asRecord(event.payload);
    return payload?.ok === false ? "danger" : "success";
  }
  if (kind === "run_finished") return "success";
  return "neutral";
}
