import { Button, Field, Icon, Input, ScrollArea } from "@botai/design";
import { type KeyboardEvent, useId, useState } from "react";
import type { University } from "@/api/types";

/** Названия — подсказки, а не готовые интеграции. Выбор хранится независимо от результатов поиска. */
export function UniversityPicker({
  query,
  onQuery,
  items,
  selected,
  onSelect,
  own,
  onOwn,
  busy,
  loading,
  error,
}: {
  query: string;
  onQuery: (value: string) => void;
  items: readonly University[];
  selected: University | null;
  onSelect: (value: University | null) => void;
  own: boolean;
  onOwn: () => void;
  busy: boolean;
  loading: boolean;
  error?: string;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const choose = (index: number) => {
    if (loading) return;
    if (index === items.length) onOwn();
    else if (items[index]) onSelect(items[index]);
    setOpen(false);
    setActive(-1);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setOpen(true);
      const next = event.key === "ArrowDown" ? Math.min(active + 1, items.length) : Math.max(active - 1, 0);
      setActive(next);
      document.getElementById(`${id}-${next}`)?.scrollIntoView?.({ block: "nearest" });
    } else if (event.key === "Enter" && open && active >= 0) {
      event.preventDefault();
      choose(active);
    } else if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
    }
  };
  return (
    <fieldset
      aria-label="Выбор вуза"
      className="relative min-w-0"
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
      }}
    >
      <Field
        label="Вуз"
        hint="Название, сокращение или привычное вам имя. Можно указать вуз, которого нет в списке."
        error={error}
      >
        <Input
          role="combobox"
          aria-expanded={open}
          aria-controls={id}
          aria-autocomplete="list"
          aria-activedescendant={open && active >= 0 ? `${id}-${active}` : undefined}
          leadingIcon="search"
          className="min-h-11"
          value={selected && !open ? selected.shortTitle : own && !open ? "Другой вуз" : query}
          onFocus={() => setOpen(true)}
          onChange={(event) => {
            onQuery(event.target.value);
            onSelect(null);
            setActive(-1);
            setOpen(true);
          }}
          onKeyDown={onKeyDown}
          placeholder="Название или сокращение"
          autoComplete="off"
          disabled={busy}
        />
      </Field>
      {open && (
        <div className="absolute inset-x-0 top-full z-layer mt-2 rounded-lg border border-border bg-surface-raised p-1 shadow-md">
          <ScrollArea className="max-h-64" viewportClassName="max-h-64">
            <div id={id} role="listbox" aria-label="Вузы" aria-busy={loading}>
              {loading ? (
                <p className="px-3 py-3 type-caption text-fg-muted" role="status">
                  Ищу вузы
                </p>
              ) : (
                <>
                  {items.map((university, index) => (
                    <Button
                      key={university.id}
                      id={`${id}-${index}`}
                      role="option"
                      aria-selected={selected?.id === university.id}
                      tabIndex={-1}
                      variant="ghost"
                      onClick={() => choose(index)}
                      className={`h-auto min-h-11 w-full justify-start whitespace-normal px-3 py-3 text-left ${active === index ? "bg-hover" : ""}`}
                    >
                      <Icon name="university" size={20} />
                      <span className="flex min-w-0 flex-col gap-1">
                        <span className="type-subheading text-fg">{university.shortTitle}</span>
                        <span className="type-caption text-fg-muted">{university.title}</span>
                      </span>
                    </Button>
                  ))}
                  <Button
                    id={`${id}-${items.length}`}
                    role="option"
                    aria-selected={own}
                    tabIndex={-1}
                    variant="ghost"
                    onClick={() => choose(items.length)}
                    className={`h-auto min-h-11 w-full justify-start whitespace-normal px-3 py-3 text-left ${active === items.length ? "bg-hover" : ""}`}
                  >
                    <Icon name="plus" size={20} />
                    <span>{query.trim() ? `Указать свой вуз: «${query.trim()}»` : "Другой вуз"}</span>
                  </Button>
                </>
              )}
            </div>
          </ScrollArea>
        </div>
      )}
    </fieldset>
  );
}
