import { MonogramBadge, Sheet, Switch } from "@botai/design";
import type { PanelOut } from "@/api/panels";
import { groupBySpace } from "./model";
import { usePatchPanel } from "./use-panels";

const TYPE_LABEL: Record<string, string> = {
  kpi: "Показатели",
  chart: "График",
  table: "Таблица",
  ranking: "Рейтинг",
  profile: "Профиль",
  list: "Список",
};

/**
 * Настройка панелей области (D-039): все панели, в том числе выключенные, с двумя переключателями.
 * «Показывать» — видимость на сервере; «Показывать в “Все вузы”» — `hiddenInAll` наоборот. Изменения применяются
 * сразу, без «Сохранить» (styleguide §7).
 */
export function PanelSettingsSheet({
  open,
  onOpenChange,
  panels,
  grouped,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  panels: readonly PanelOut[];
  grouped: boolean;
}) {
  const patch = usePatchPanel();
  const groups = groupBySpace(panels);
  return (
    <Sheet
      open={open}
      onOpenChange={onOpenChange}
      title="Панели успеваемости"
      description="Выберите, что показывать на экране «Учёба». Данные не удаляются: выключенную панель можно вернуть."
      size="full"
    >
      <div className="flex flex-col gap-5">
        {groups.length === 0 && <p className="type-ui text-fg-muted">Панелей пока нет.</p>}
        {groups.map((group) => (
          <section key={group.space.id} className="flex flex-col gap-2">
            {grouped && (
              <h3 className="flex items-center gap-2 type-subheading text-fg">
                <MonogramBadge
                  size="sm"
                  title={group.space.title}
                  shortTitle={group.space.shortTitle}
                  hue={group.space.accentHue}
                  decorative
                />
                {group.space.title}
              </h3>
            )}
            <ul className="flex flex-col gap-2">
              {group.panels.map((panel) => (
                <li key={panel.id}>
                  <fieldset className="flex min-w-0 flex-col rounded-lg border border-border bg-surface px-3 py-2">
                    <legend className="px-1 type-label text-fg">
                      {panel.spec.title}
                      <span className="ml-2 type-caption font-normal text-fg-muted">
                        {TYPE_LABEL[panel.type] ?? "Панель"}
                      </span>
                    </legend>
                    <Switch
                      label="Показывать"
                      checked={panel.visible}
                      onCheckedChange={(visible) => patch.mutate({ id: panel.id, patch: { visible } })}
                    />
                    <Switch
                      label="Показывать в «Все вузы»"
                      checked={!panel.hiddenInAll}
                      disabled={!panel.visible}
                      onCheckedChange={(shown) =>
                        patch.mutate({ id: panel.id, patch: { hiddenInAll: !shown } })
                      }
                    />
                  </fieldset>
                </li>
              ))}
            </ul>
          </section>
        ))}
      </div>
    </Sheet>
  );
}
