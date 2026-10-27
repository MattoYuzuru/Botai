import { type ReactNode, useState } from "react";
import { ALL_SPACES_ID } from "../../lib/accents";
import { cn } from "../../lib/cn";
import { Button } from "../button/button";
import { Icon } from "../icon";
import { Sheet } from "../sheet/sheet";
import { type SourceState, SourceStatus } from "../source-status/source-status";
import { choiceRow } from "../styles";

export interface SwitcherSpace {
  id: string;
  title: string;
  shortTitle?: string;
  /** Тон акцента вуза — для справки; цветом вузы в списке не различаются. */
  hue?: number;
  status: SourceState;
  updatedAt: number | null;
}

export interface UniversitySpaceSwitcherProps {
  spaces: readonly SwitcherSpace[];
  /** Идентификатор активного пространства или `all` для сводного экрана. */
  activeId: string;
  onChange: (id: string) => void;
  /** Показывать пункт «Все вузы». */
  allowAll?: boolean;
  nowMs: number;
  timeZone?: string;
  className?: string;
}

/**
 * Кнопка в шапке → шит со списком вузов. У каждого вуза строка `SourceStatus`. Сводный экран
 * различает вузы подписью и меткой, а не цветом.
 */
export function UniversitySpaceSwitcher({
  spaces,
  activeId,
  onChange,
  allowAll = false,
  nowMs,
  timeZone,
  className,
}: UniversitySpaceSwitcherProps) {
  const [open, setOpen] = useState(false);
  const active = spaces.find((space) => space.id === activeId);
  const label =
    activeId === ALL_SPACES_ID ? "Все вузы" : (active?.shortTitle ?? active?.title ?? "Выберите вуз");

  const choose = (id: string) => {
    setOpen(false);
    if (id !== activeId) onChange(id);
  };

  return (
    <>
      <Button
        variant="ghost"
        size="sm"
        data-touch-expanded=""
        trailingIcon="chevron-down"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen(true)}
        className={cn("max-w-48", className)}
      >
        <Icon name="university" size={20} />
        <span className="truncate">{label}</span>
      </Button>
      <Sheet
        open={open}
        onOpenChange={setOpen}
        title="Вуз"
        description="Расписание и задания покажутся для выбранного вуза."
      >
        <ul className="flex flex-col gap-0.5" aria-label="Вузы">
          {allowAll && (
            <li>
              <SpaceRow
                title="Все вузы"
                selected={activeId === ALL_SPACES_ID}
                onSelect={() => choose(ALL_SPACES_ID)}
                caption="Пары и сроки из всех пространств"
              />
            </li>
          )}
          {spaces.map((space) => (
            <li key={space.id}>
              <SpaceRow
                title={space.title}
                selected={space.id === activeId}
                onSelect={() => choose(space.id)}
                caption={
                  <SourceStatus
                    state={space.status}
                    spaceId={space.id}
                    spaceTitle={space.shortTitle ?? space.title}
                    updatedAt={space.updatedAt}
                    nowMs={nowMs}
                    timeZone={timeZone}
                  />
                }
              />
            </li>
          ))}
        </ul>
      </Sheet>
    </>
  );
}

function SpaceRow({
  title,
  caption,
  selected,
  onSelect,
}: {
  title: string;
  caption: ReactNode;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      aria-current={selected ? "true" : undefined}
      onClick={onSelect}
      className={cn(choiceRow, "items-center", selected && "bg-accent-subtle")}
    >
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="type-subheading text-fg">{title}</span>
        <span className="min-w-0 type-caption text-fg-muted">{caption}</span>
      </span>
      {selected && <Icon name="check" size={20} className="text-accent-fg" />}
    </button>
  );
}
