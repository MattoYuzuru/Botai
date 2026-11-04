import { Button, Icon } from "@botai/design";
import type { PanelOut } from "@/api/panels";
import { readPanel } from "./model";
import { PanelView } from "./panel-view";
import { useDecideProposal } from "./use-panels";

/**
 * Предложение агента добавить панель (D-039): что за панель, как она выглядит на реальных данных и два
 * действия. Предпросмотр — та же панель из реестра, без своих кнопок. Панель, которую клиент не умеет рисовать,
 * не предлагается: показать вслепую нечего.
 */
export function ProposedPanelCard({ panel, zone }: { panel: PanelOut; zone: string }) {
  const accept = useDecideProposal("accept");
  const dismiss = useDecideProposal("dismiss");
  if (!readPanel(panel)) return null;
  const busy = accept.isPending || dismiss.isPending;
  const titleId = `proposal-${panel.id}`;
  return (
    <article
      aria-labelledby={titleId}
      data-proposed-panel={panel.id}
      className="flex min-w-0 flex-col gap-3 rounded-lg border border-dashed border-border-strong bg-surface p-4"
    >
      <div className="flex items-start gap-3">
        <Icon name="chat" size={20} className="mt-1 shrink-0 text-fg-subtle" />
        <div className="flex min-w-0 flex-col gap-1">
          <h3 id={titleId} className="type-heading text-fg">
            Агент предлагает добавить панель «{panel.spec.title}»
          </h3>
          <p className="type-caption text-fg-muted">
            Так она будет выглядеть на ваших данных. Добавленную панель можно выключить в настройках.
          </p>
        </div>
      </div>
      <div
        role="img"
        aria-label={`Предпросмотр панели «${panel.spec.title}»`}
        className="pointer-events-none max-h-60 select-none overflow-hidden rounded-lg [mask-image:linear-gradient(to_bottom,black_80%,transparent)]"
      >
        <div aria-hidden="true" inert>
          <PanelView panel={panel} context={{ zone, preview: true }} headingLevel={4} />
        </div>
      </div>
      <div className="flex flex-wrap gap-2">
        <Button
          variant="primary"
          disabled={busy}
          onClick={() => accept.mutate(panel.id)}
          aria-label={`Добавить панель «${panel.spec.title}»`}
        >
          Добавить
        </Button>
        <Button
          variant="ghost"
          disabled={busy}
          onClick={() => dismiss.mutate(panel.id)}
          aria-label={`Не сейчас: панель «${panel.spec.title}»`}
        >
          Не сейчас
        </Button>
      </div>
    </article>
  );
}
