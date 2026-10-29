import { DropdownMenu } from "radix-ui";
import {
  type ClipboardEvent,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
  useRef,
  useState,
} from "react";
import { cn } from "../../lib/cn";
import { countRu } from "../../lib/format";
import { useMediaQuery } from "../../lib/hooks";
import { useAutoGrow } from "../../lib/use-auto-grow";
import { formatRecordingTime, useVoiceRecorder } from "../../lib/use-voice-recorder";
import { AttachmentChip } from "../attachment-chip/attachment-chip";
import type { ComposerAttachment, FileSource } from "../attachments";
import { Button } from "../button/button";
import { Icon, type IconName } from "../icon";
import { IconButton } from "../icon-button/icon-button";
import { floatingSurface } from "../styles";

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
  /** Запись голоса. Кнопка скрыта, если браузер не умеет записывать звук. */
  voice?: boolean;
  className?: string;
}

const VOICE_ERRORS = {
  denied: "Нет доступа к микрофону. Разрешите его в настройках браузера.",
  unsupported: "Запись голоса не поддерживается в этом браузере.",
  failed: "Не удалось записать звук. Проверьте микрофон.",
} as const;

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
 * микрофон (пока нет ни текста, ни вложений) или «Отправить». «+» открывает выбор файла; на устройствах с касанием
 * это меню «Файл / Фото / Снять фото». Файлы можно вставить из буфера. Голос записывается `MediaRecorder` и
 * прикрепляется чипом, расшифровки нет. Варя сидит на верхней кромке справа (`mascotSlot`) и не перекрывает кнопки.
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
  voice: voiceEnabled = true,
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
  const recorder = useVoiceRecorder();
  const [announcement, setAnnouncement] = useState("");

  const text = value.trim();
  const uploading = attachments.some((item) => item.status === "uploading");
  const ready = attachments.some((item) => item.status === "ready");
  const canSend = !disabled && !uploading && (text.length > 0 || ready);
  const full = attachments.length >= maxAttachments;
  const recording = recorder.state.status === "recording";
  const canAttach = Boolean(onAddFiles) && !disabled;
  const showMic =
    voiceEnabled &&
    recorder.supported &&
    Boolean(onAddFiles) &&
    text.length === 0 &&
    attachments.length === 0;

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
    if (await recorder.start()) setAnnouncement("Запись началась");
  };

  const stopRecording = async () => {
    const file = await recorder.stop();
    setAnnouncement("Запись остановлена");
    if (file) add([file], "voice");
  };

  const cancelRecording = () => {
    recorder.cancel();
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

  const voiceError = recorder.state.status === "error" ? VOICE_ERRORS[recorder.state.reason] : null;

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
        className="sr-only"
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
        className="sr-only"
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
        className="sr-only"
        onChange={(event) => add(pickedFiles(event.target), "camera")}
      />
      <span role="status" aria-live="polite" className="sr-only">
        {announcement}
      </span>
      <div
        className={cn(
          "flex flex-col gap-1 rounded-xl border border-border-strong bg-surface p-1.5 shadow-sm",
          "transition-colors duration-base ease-soft hover:border-control",
          "focus-within:border-focus-ring focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-focus-ring",
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
            <Button variant="ghost" size="sm" onClick={recorder.clearError}>
              Закрыть
            </Button>
          </div>
        )}
        {recording ? (
          <div data-recording="" className="flex items-center gap-1">
            <IconButton icon="close" label="Отменить запись" onClick={cancelRecording} className="shrink-0" />
            <div className="flex min-h-11 min-w-0 flex-1 items-center gap-2 px-2">
              <span
                aria-hidden="true"
                className="size-2 shrink-0 animate-pulse-soft rounded-full bg-danger-fg"
              />
              <span className="truncate type-body text-fg" data-numeric="">
                {`Идёт запись · ${formatRecordingTime(
                  recorder.state.status === "recording" ? recorder.state.elapsedMs : 0,
                )}`}
              </span>
            </div>
            <IconButton
              icon="check"
              label="Остановить и прикрепить"
              variant="primary"
              shape="round"
              onClick={() => void stopRecording()}
              className="shrink-0"
            />
          </div>
        ) : null}
        <div className={cn("flex items-end gap-1", recording && "hidden")}>
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
          {showMic ? (
            <IconButton
              icon="mic"
              label="Записать голосовое сообщение"
              variant="secondary"
              shape="round"
              disabled={disabled}
              onClick={() => void startRecording()}
              className="shrink-0"
            />
          ) : (
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
