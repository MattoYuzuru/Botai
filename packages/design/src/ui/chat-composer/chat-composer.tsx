import { DropdownMenu } from "radix-ui";
import {
  type ClipboardEvent,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
  useEffect,
  useRef,
  useState,
} from "react";
import { cn } from "../../lib/cn";
import { countRu } from "../../lib/format";
import { useMediaQuery } from "../../lib/hooks";
import { useAutoGrow } from "../../lib/use-auto-grow";
import { formatRecordingTime, type Transcribe, useDictation } from "../../lib/use-dictation";
import { AttachmentChip } from "../attachment-chip/attachment-chip";
import type { ComposerAttachment, FileSource } from "../attachments";
import { Button } from "../button/button";
import { Icon, type IconName } from "../icon";
import { IconButton } from "../icon-button/icon-button";
import { floatingSurface } from "../styles";
import { VoiceEqualizer } from "../voice-equalizer/voice-equalizer";

export const DEFAULT_MAX_ATTACHMENTS = 10;

export interface ChatComposerProps {
  value: string;
  onValueChange: (value: string) => void;
  /** Отправка. Текст уже без краёв и может быть пустым, если есть готовое вложение. Поле очищает вызывающий код. */
  onSend: (text: string) => void;
  disabled?: boolean;
  placeholder?: string;
  /** Насест Вари: стоит на верхней кромке поля справа, лапы на линии рамки (mascot.md §4.1). */
  mascotSlot?: ReactNode;
  /** Enter отправляет (по умолчанию — на устройствах с мышью); на телефоне Enter — перенос строки. */
  enterToSend?: boolean;
  /** Подпись поля для скринридера. */
  label?: string;
  /** Вложения, ещё не отправленные: загрузка идёт у вызывающего кода. */
  attachments?: readonly ComposerAttachment[];
  /** Файлы пришли из выбора, камеры, буфера обмена или записи; вызывающий код грузит их и добавляет в `attachments`. */
  onAddFiles?: (files: File[], source: FileSource) => void;
  onRemoveAttachment?: (id: string) => void;
  onOpenAttachment?: (id: string) => void;
  /** Не больше этого числа вложений в одном сообщении. */
  maxAttachments?: number;
  /** Строка над полем: «Ждёт вашего ответа» и тому подобное. */
  notice?: ReactNode;
  /**
   * Диктовка: запись голоса превращается в текст этой функцией и вставляется в поле (сообщение не отправляется).
   * Нет функции или браузер не умеет записывать звук — кнопки микрофона нет.
   */
  onTranscribe?: Transcribe;
  /** Смена значения (другой чат) бросает идущую запись: чужой голос не попадёт в поле другого чата. */
  resetKey?: string;
  className?: string;
}

function pickedFiles(input: HTMLInputElement): File[] {
  const files = Array.from(input.files ?? []);
  // Тот же файл можно выбрать повторно после «Убрать».
  input.value = "";
  return files;
}

const menuItem =
  "flex min-h-11 w-full cursor-pointer select-none items-center gap-3 rounded-md px-3 py-2.5 type-ui text-fg outline-none data-[highlighted]:bg-hover";

/**
 * Поле ввода чата. Карточка: чипы вложений, затем строка «+» · `Textarea` 16 px с авто-ростом до 6 строк ·
 * микрофон · «Отправить» (пока нет ни текста, ни вложений, остаётся один микрофон). «+» открывает выбор файла; на
 * устройствах с касанием это меню «Файл / Фото / Снять фото». Файлы можно вставить из буфера. Микрофон — диктовка:
 * вместо поля — эквалайзер, таймер, «Отменить запись» и «Готово»; после «Готово» речь распознаётся (`onTranscribe`),
 * текст вставляется в конец поля через пробел и поле получает фокус. Сообщение не отправляется само: текст можно
 * поправить. Звук не становится вложением и нигде не сохраняется. Варя сидит на верхней кромке справа (`mascotSlot`).
 */
export function ChatComposer({
  value,
  onValueChange,
  onSend,
  disabled = false,
  placeholder = "Спросите про пары и сроки",
  mascotSlot,
  enterToSend,
  label = "Сообщение агенту",
  attachments = [],
  onAddFiles,
  onRemoveAttachment,
  onOpenAttachment,
  maxAttachments = DEFAULT_MAX_ATTACHMENTS,
  notice,
  onTranscribe,
  resetKey,
  className,
}: ChatComposerProps) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const photoInput = useRef<HTMLInputElement>(null);
  const cameraInput = useRef<HTMLInputElement>(null);
  const finePointer = useMediaQuery("(pointer: fine)");
  const coarse = useMediaQuery("(pointer: coarse)");
  const sendOnEnter = enterToSend ?? finePointer;
  const resize = useAutoGrow(ref, value || placeholder, 6);
  // Текст из распознавания дописывается к тому, что в поле на тот момент, а не на момент нажатия микрофона.
  const latestValue = useRef(value);
  latestValue.current = value;
  const doneButton = useRef<HTMLButtonElement>(null);
  const [announcement, setAnnouncement] = useState("");
  const dictation = useDictation({
    transcribe: onTranscribe,
    onText: (spoken) => {
      const current = latestValue.current.replace(/\s+$/, "");
      onValueChange(current ? `${current} ${spoken}` : spoken);
      setAnnouncement("Текст добавлен в поле");
    },
  });
  const cancelDictation = dictation.cancel;
  const lastReset = useRef(resetKey);
  useEffect(() => {
    if (lastReset.current === resetKey) return;
    lastReset.current = resetKey;
    cancelDictation();
  }, [resetKey, cancelDictation]);
  const phase = dictation.phase;
  const dictating = phase.status === "recording" || phase.status === "transcribing";
  // Фокус идёт за состоянием: запись началась — на «Готово» (кнопка микрофона исчезла, фокус не должен
  // пропасть); запись или распознавание кончились (текст вставлен, отмена, ошибка) — в поле, курсор после текста.
  const was = useRef(phase.status);
  useEffect(() => {
    const before = was.current;
    was.current = phase.status;
    if (before === phase.status) return;
    if (phase.status === "recording") {
      doneButton.current?.focus();
      return;
    }
    const left = before === "recording" || before === "transcribing";
    const field = ref.current;
    if (left && (phase.status === "idle" || phase.status === "error") && field) {
      field.focus();
      field.setSelectionRange(field.value.length, field.value.length);
    }
  }, [phase.status]);

  const text = value.trim();
  const uploading = attachments.some((item) => item.status === "uploading");
  const ready = attachments.some((item) => item.status === "ready");
  const canSend = !disabled && !uploading && (text.length > 0 || ready);
  const full = attachments.length >= maxAttachments;
  const canAttach = Boolean(onAddFiles) && !disabled;
  // Пока в поле пусто, правая кнопка — один микрофон; с текстом или вложением рядом с ним появляется «Отправить».
  const micOnly = dictation.supported && text.length === 0 && attachments.length === 0;

  const add = (files: File[], source: FileSource) => {
    if (!onAddFiles || files.length === 0) return;
    const room = Math.max(0, maxAttachments - attachments.length);
    if (room > 0) onAddFiles(files.slice(0, room), source);
  };

  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    if (canSend) onSend(text);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing || !sendOnEnter) return;
    event.preventDefault();
    submit();
  };

  const onPaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    const files = Array.from(event.clipboardData?.files ?? []);
    if (files.length === 0 || !onAddFiles) return;
    event.preventDefault();
    add(files, "paste");
  };

  const startRecording = async () => {
    if (await dictation.start()) setAnnouncement("Запись началась");
  };

  const finishRecording = async () => {
    setAnnouncement("Распознаю речь");
    await dictation.finish();
  };

  const cancelRecording = () => {
    dictation.cancel();
    setAnnouncement("Запись отменена");
  };

  const pick = (input: RefObject<HTMLInputElement | null>) => input.current?.click();

  const attachButton = (
    <IconButton
      icon="attach"
      label="Прикрепить файл"
      disabled={!canAttach || full}
      onClick={coarse ? undefined : () => pick(fileInput)}
      className="shrink-0"
    />
  );

  const menuEntries: { icon: IconName; label: string; input: RefObject<HTMLInputElement | null> }[] = [
    { icon: "file", label: "Файл", input: fileInput },
    { icon: "image", label: "Фото", input: photoInput },
    { icon: "camera", label: "Снять фото", input: cameraInput },
  ];

  const voiceError = phase.status === "error" ? phase.message : null;

  return (
    <form onSubmit={submit} data-composer="" className={cn("relative z-dock", className)}>
      {mascotSlot && (
        <div
          data-perch=""
          className="pointer-events-none absolute right-4 bottom-full z-dock translate-y-px *:pointer-events-auto"
        >
          {mascotSlot}
        </div>
      )}
      <input
        ref={fileInput}
        type="file"
        multiple
        tabIndex={-1}
        aria-hidden="true"
        data-composer-file=""
        className="sr-only type-body"
        onChange={(event) => add(pickedFiles(event.target), "picker")}
      />
      <input
        ref={photoInput}
        type="file"
        multiple
        accept="image/*"
        tabIndex={-1}
        aria-hidden="true"
        data-composer-photo=""
        className="sr-only type-body"
        onChange={(event) => add(pickedFiles(event.target), "picker")}
      />
      <input
        ref={cameraInput}
        type="file"
        accept="image/*"
        capture="environment"
        tabIndex={-1}
        aria-hidden="true"
        data-composer-camera=""
        className="sr-only type-body"
        onChange={(event) => add(pickedFiles(event.target), "camera")}
      />
      <span role="status" aria-live="polite" className="sr-only">
        {announcement}
      </span>
      <div
        data-composer-surface=""
        className={cn(
          "flex flex-col gap-1 rounded-xl border border-border-strong bg-surface p-1.5 shadow-sm",
          "transition-colors duration-base ease-soft hover:border-control",
          "focus-within:border-focus-ring",
        )}
      >
        {notice && <div className="px-3 pt-1.5 type-caption text-fg-muted">{notice}</div>}
        {attachments.length > 0 && (
          <ul aria-label="Вложения" className="flex flex-wrap gap-2 px-1.5 pt-1.5">
            {attachments.map((attachment) => (
              <li key={attachment.id} className="flex max-w-full min-w-0">
                <AttachmentChip
                  attachment={attachment}
                  onRemove={onRemoveAttachment}
                  onOpen={onOpenAttachment}
                />
              </li>
            ))}
          </ul>
        )}
        {full && (
          <p className="px-3 type-caption text-fg-muted">
            {`К сообщению можно прикрепить не больше ${countRu(maxAttachments, ["файла", "файлов", "файлов"])}.`}
          </p>
        )}
        {voiceError && (
          <div role="alert" className="flex items-start gap-2 px-3 py-1 type-caption text-danger-fg">
            <Icon name="warning" size={16} className="mt-0.5" />
            <span className="min-w-0 flex-1">{voiceError}</span>
            <Button variant="ghost" size="sm" onClick={dictation.clearError}>
              Закрыть
            </Button>
          </div>
        )}
        {dictating ? (
          <div data-recording="" data-phase={phase.status} className="flex items-center gap-1">
            <IconButton
              icon="close"
              label={phase.status === "recording" ? "Отменить запись" : "Отменить распознавание"}
              onClick={cancelRecording}
              className="shrink-0"
            />
            {phase.status === "recording" ? (
              <div className="flex min-h-11 min-w-0 flex-1 items-center justify-center gap-3 px-2">
                <span className="sr-only">Идёт запись</span>
                <VoiceEqualizer meter={dictation.meter} className="max-w-64" />
                <span className="shrink-0 type-body text-fg" data-numeric="" data-recording-time="">
                  {formatRecordingTime(phase.elapsedMs)}
                </span>
              </div>
            ) : (
              <div className="flex min-h-11 min-w-0 flex-1 items-center px-2">
                <span className="truncate type-body text-fg-muted">Распознаю…</span>
              </div>
            )}
            <IconButton
              ref={doneButton}
              icon="check"
              label="Готово"
              variant="primary"
              shape="round"
              loading={phase.status === "transcribing"}
              onClick={() => void finishRecording()}
              className="shrink-0"
            />
          </div>
        ) : null}
        <div className={cn("flex items-end gap-1", dictating && "hidden")}>
          {onAddFiles &&
            (coarse ? (
              <DropdownMenu.Root>
                <DropdownMenu.Trigger asChild>{attachButton}</DropdownMenu.Trigger>
                <DropdownMenu.Portal>
                  <DropdownMenu.Content
                    side="top"
                    align="start"
                    sideOffset={8}
                    className={cn(floatingSurface, "min-w-48 rounded-lg p-1")}
                  >
                    {menuEntries.map((entry) => (
                      <DropdownMenu.Item
                        key={entry.label}
                        onSelect={() => pick(entry.input)}
                        className={menuItem}
                      >
                        <Icon name={entry.icon} size={20} className="text-fg-muted" />
                        {entry.label}
                      </DropdownMenu.Item>
                    ))}
                  </DropdownMenu.Content>
                </DropdownMenu.Portal>
              </DropdownMenu.Root>
            ) : (
              attachButton
            ))}
          <textarea
            ref={ref}
            value={value}
            rows={1}
            disabled={disabled}
            aria-label={label}
            placeholder={placeholder}
            enterKeyHint={sendOnEnter ? "send" : "enter"}
            onChange={(event) => onValueChange(event.target.value)}
            onInput={resize}
            onKeyDown={onKeyDown}
            onPaste={onPaste}
            className="min-h-11 min-w-0 flex-1 resize-none bg-transparent px-2 py-2.5 type-body text-fg outline-none placeholder:text-fg-subtle disabled:cursor-not-allowed"
          />
          {dictation.supported && (
            <IconButton
              icon="mic"
              label="Надиктовать текст"
              variant={micOnly ? "secondary" : "ghost"}
              shape="round"
              disabled={disabled}
              onClick={() => void startRecording()}
              className="shrink-0"
            />
          )}
          {!micOnly && (
            <IconButton
              type="submit"
              icon="send"
              label="Отправить"
              variant="primary"
              shape="round"
              disabled={!canSend}
              className="shrink-0"
            />
          )}
        </div>
      </div>
    </form>
  );
}
