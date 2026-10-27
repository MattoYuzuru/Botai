import {
  Banner,
  Button,
  Field,
  FilePicker,
  formatBytes,
  Input,
  ProgressRail,
  SegmentedControl,
  Select,
  Sheet,
  Textarea,
  useNow,
} from "@botai/design";
import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { humanMessage } from "@/api/errors";
import { trackJob } from "@/features/data/use-reconnect";
import type { SpaceInfo } from "@/features/data/use-space-directory";
import {
  MAX_UPLOAD_BYTES,
  startUploadDocument,
  startUrlDocument,
  UPLOAD_ACCEPT,
  validateFile,
  validateUrl,
} from "./add-document";
import { refreshAfterChange } from "./connection-actions";
import { describeJob, isJobFinished, useIngestJob } from "./use-ingest-job";

type Mode = "url" | "file";

export interface AddDocumentSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Вузы, в которые можно добавить документ. */
  spaces: readonly SpaceInfo[];
  /** Вуз по умолчанию (экран раздела одного вуза). */
  defaultSpaceId?: string;
}

/**
 * «Добавить документ»: ссылка (файл, страница, Google-документ или таблица по ссылке) или файл до 8 МБ. После
 * отправки видно задание чтения — стадия словами и настоящее время, без процентов. Закрыть шит можно в любой
 * момент: задание идёт на сервере, документ появится в списке сам.
 */
export function AddDocumentSheet({ open, onOpenChange, spaces, defaultSpaceId }: AddDocumentSheetProps) {
  const queryClient = useQueryClient();
  const [mode, setMode] = useState<Mode>("url");
  const [spaceId, setSpaceId] = useState<string | undefined>(defaultSpaceId);
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [note, setNote] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState<string | undefined>();
  const [busy, setBusy] = useState(false);
  const [started, setStarted] = useState<{
    jobId: string;
    title: string;
    spaceId: string;
    connectionId: string;
  } | null>(null);

  const target = spaces.find((space) => space.id === (spaceId ?? defaultSpaceId)) ?? spaces[0];
  const job = useIngestJob(started?.jobId ?? null);
  const finished = isJobFinished(job.data);

  const reset = () => {
    setMode("url");
    setUrl("");
    setTitle("");
    setNote("");
    setFile(null);
    setError(undefined);
    setStarted(null);
  };

  const close = (next: boolean) => {
    onOpenChange(next);
    if (!next) {
      // Документ мог появиться, пока шит был открыт: обновляем списки и счётчики.
      if (target) void refreshAfterChange(queryClient, target.id);
      // Форму очищаем после анимации закрытия.
      window.setTimeout(reset, 300);
    }
  };

  const submit = async () => {
    if (!target || busy) return;
    const problem = mode === "url" ? validateUrl(url) : file ? validateFile(file) : "Выберите файл";
    if (problem) {
      setError(problem);
      return;
    }
    setError(undefined);
    setBusy(true);
    try {
      const result =
        mode === "url"
          ? await startUrlDocument(target.id, { url, title, note })
          : await startUploadDocument(target.id, file as File, note);
      setStarted({
        jobId: result.jobId,
        title: (mode === "url" ? title.trim() || url.trim() : (file as File).name) || "Документ",
        spaceId: target.id,
        connectionId: result.connectionId,
      });
      void refreshAfterChange(queryClient, target.id);
    } catch (cause) {
      setError(humanMessage(cause));
    } finally {
      setBusy(false);
    }
  };

  const footer = started ? (
    <Button variant={finished ? "primary" : "secondary"} fullWidth onClick={() => close(false)}>
      {finished ? "Готово" : "Закрыть"}
    </Button>
  ) : (
    <Button variant="primary" fullWidth type="submit" form="add-document-form" loading={busy}>
      Добавить
    </Button>
  );

  return (
    <Sheet
      open={open}
      onOpenChange={close}
      title="Добавить документ"
      description="BotAI прочитает документ и будет отвечать по нему. Пароли и личные данные в него лучше не класть."
      footer={footer}
      size="auto"
    >
      {started ? (
        <IngestProgress
          title={started.title}
          jobId={started.jobId}
          job={job.data}
          space={spaces.find((space) => space.id === started.spaceId)}
          connectionId={started.connectionId}
          onAnswer={() => close(false)}
        />
      ) : (
        <form
          id="add-document-form"
          className="flex flex-col gap-4 pb-2"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
          noValidate
        >
          <SegmentedControl<Mode>
            label="Как добавить"
            value={mode}
            onValueChange={(value) => {
              setMode(value);
              setError(undefined);
            }}
            options={[
              { value: "url", label: "Ссылка", icon: "source" },
              { value: "file", label: "Файл", icon: "document" },
            ]}
          />

          {spaces.length > 1 && (
            <Field label="В какой вуз добавить">
              <Select
                value={target?.id}
                onValueChange={setSpaceId}
                sheetTitle="Вуз"
                options={spaces.map((space) => ({ value: space.id, label: space.title }))}
              />
            </Field>
          )}

          {mode === "url" ? (
            <>
              <Field
                label="Ссылка"
                hint="Файл, страница сайта или Google-документ и таблица, открытые по ссылке"
                error={error}
              >
                <Input
                  type="url"
                  inputMode="url"
                  autoComplete="off"
                  autoCapitalize="off"
                  spellCheck={false}
                  placeholder="https://…"
                  value={url}
                  onChange={(event) => {
                    setUrl(event.target.value);
                    setError(undefined);
                  }}
                />
              </Field>
              <Field label="Название" optional>
                <Input value={title} maxLength={200} onChange={(event) => setTitle(event.target.value)} />
              </Field>
            </>
          ) : (
            <Field
              label="Файл"
              hint={`PDF, DOCX, XLSX, CSV, HTML или текст, до ${formatBytes(MAX_UPLOAD_BYTES)}`}
              error={error}
            >
              <FilePicker
                value={file}
                accept={UPLOAD_ACCEPT}
                onValueChange={(next) => {
                  setFile(next);
                  setError(undefined);
                }}
              />
            </Field>
          )}

          <Field
            label="Что это за документ"
            hint="Своими словами: поможет агенту разобрать таблицу или текст"
            optional
          >
            <Textarea
              minRows={2}
              maxLength={1000}
              value={note}
              onChange={(event) => setNote(event.target.value)}
            />
          </Field>
        </form>
      )}
    </Sheet>
  );
}

function IngestProgress({
  title,
  jobId,
  job,
  space,
  connectionId,
  onAnswer,
}: {
  title: string;
  jobId: string;
  job: ReturnType<typeof useIngestJob>["data"];
  space: SpaceInfo | undefined;
  connectionId: string;
  onAnswer: () => void;
}) {
  const nowMs = useNow(1000);
  const navigate = useNavigate();
  const view = describeJob(job);
  const startedAt = job ? Date.parse(job.createdAt) : nowMs;
  return (
    <div className="flex flex-col gap-4 pb-2" data-ingest-state={job?.state ?? "starting"}>
      <p className="break-words type-subheading text-fg">{title}</p>
      {view.failed ? (
        <Banner tone="danger" icon="error" live>
          <span className="flex flex-col gap-1">
            <span className="type-ui">{view.text}</span>
            <span className="type-caption">
              Проверьте, что ссылка открывается без входа, и добавьте документ ещё раз.
            </span>
          </span>
        </Banner>
      ) : view.done ? (
        <Banner tone="success" icon="ok" live>
          Документ добавлен. Он появится в списке документов и в ответах чата.
        </Banner>
      ) : (
        <div className="flex flex-col gap-3" role="status">
          <p className="type-ui text-fg">{view.text}</p>
          <ProgressRail
            stage={0}
            elapsedMs={Math.max(0, nowMs - startedAt)}
            etaKnown={false}
            className="w-full"
          />
          <p className="type-caption text-fg-muted">Шит можно закрыть: документ добавится сам.</p>
        </div>
      )}
      {view.asking && (
        <Button
          variant="primary"
          onClick={() => {
            trackJob({
              jobId,
              title: `Документ: ${title}`,
              spaceTitle: space?.shortTitle,
              connectionId,
            });
            onAnswer();
            void navigate({ to: "/connect/$jobId", params: { jobId } });
          }}
        >
          Ответить
        </Button>
      )}
    </div>
  );
}
