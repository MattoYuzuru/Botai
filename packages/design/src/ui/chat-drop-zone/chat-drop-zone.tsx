import { type DragEvent, type ReactNode, useRef, useState } from "react";
import { cn } from "../../lib/cn";
import type { FileSource } from "../attachments";
import { Icon } from "../icon";

export interface ChatDropZoneProps {
  onFiles: (files: File[], source: FileSource) => void;
  disabled?: boolean;
  children: ReactNode;
  className?: string;
}

const carriesFiles = (event: DragEvent) => Array.from(event.dataTransfer?.types ?? []).includes("Files");

/**
 * Область чата, в которую можно перетащить файлы. Пока над ней файлы, показан оверлей «Отпустите файлы, чтобы
 * прикрепить»; перетаскиваемый текст и ссылки не затрагиваются. `dragenter`/`dragleave` приходят и от
 * вложенных элементов, поэтому оверлей держится на счётчике и не мигает.
 */
export function ChatDropZone({ onFiles, disabled = false, children, className }: ChatDropZoneProps) {
  const depth = useRef(0);
  const [active, setActive] = useState(false);

  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: область приёма файлов — только перетаскивание, действие дублирует кнопка «Прикрепить файл»
    <div
      data-drop-zone=""
      data-dragging={active || undefined}
      className={cn("flex min-h-0 flex-1 flex-col", className)}
      onDragEnter={(event) => {
        if (!carriesFiles(event)) return;
        event.preventDefault();
        if (disabled) return;
        depth.current += 1;
        setActive(true);
      }}
      onDragOver={(event) => {
        if (!carriesFiles(event)) return;
        // Без preventDefault браузер не разрешит drop и откроет файл вместо вложения.
        event.preventDefault();
        event.dataTransfer.dropEffect = disabled ? "none" : "copy";
      }}
      onDragLeave={(event) => {
        if (!carriesFiles(event) || disabled) return;
        depth.current = Math.max(0, depth.current - 1);
        if (depth.current === 0) setActive(false);
      }}
      onDrop={(event) => {
        if (!carriesFiles(event)) return;
        event.preventDefault();
        depth.current = 0;
        setActive(false);
        if (disabled) return;
        const files = Array.from(event.dataTransfer.files ?? []);
        if (files.length > 0) onFiles(files, "drop");
      }}
    >
      {children}
      {active && (
        <div
          data-drop-overlay=""
          role="presentation"
          className="pointer-events-none fixed inset-0 z-scrim animate-fade-in px-safe-4 pt-safe-4 pb-safe-4"
        >
          <div className="grid size-full place-items-center rounded-xl border-2 border-dashed border-accent-fg bg-bg/80">
            <p className="flex items-center gap-2 type-heading text-accent-fg">
              <Icon name="attach" size={24} />
              Отпустите файлы, чтобы прикрепить
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
