import { Panel } from "@botai/design";
import { createElement } from "react";
import type { PanelOut } from "@/api/panels";
import { panelIcon, readPanel } from "./model";
import { type RenderContext, renderPanelBody } from "./registry";
import { SafePanel } from "./safe-boundary";

export interface PanelViewProps {
  panel: PanelOut;
  context: RenderContext;
  headingLevel: 2 | 3 | 4;
  collapsed?: boolean;
  onCollapsedChange?: (collapsed: boolean) => void;
  /** Показатели без рамки панели: плитки идут сразу под заголовком раздела. */
  bare?: boolean;
  /** Название панели повторяет заголовок раздела: оно остаётся для скринридера, зрячим не показывается. */
  titleHidden?: boolean;
}

/** Можно ли панель нарисовать: известный тип, форма данных верна. Иначе экран её скрывает. */
export const isRenderable = (panel: PanelOut) => readPanel(panel) !== null;

/**
 * Одна панель: рамка `Panel` дизайна и тело из реестра. Неизвестный тип, испорченные данные или
 * исключение при отрисовке дают `null`: панель скрыта, экран жив.
 */
export function PanelView({
  panel,
  context,
  headingLevel,
  collapsed,
  onCollapsedChange,
  bare = false,
  titleHidden = false,
}: PanelViewProps) {
  const body = readPanel(panel);
  if (!body) return null;
  const rendered = renderPanelBody(body, panel, context);
  if (bare && rendered.state === "ready") {
    return (
      <SafePanel panelId={panel.id}>
        <section
          aria-labelledby={`bare-${panel.id}`}
          data-panel-id={panel.id}
          data-panel-type={panel.type}
          className="min-w-0"
        >
          {createElement(
            `h${headingLevel}`,
            {
              id: `bare-${panel.id}`,
              className: titleHidden ? "sr-only" : "px-1 pb-2 type-label text-fg-muted",
            },
            panel.spec.title,
          )}
          {rendered.node}
        </section>
      </SafePanel>
    );
  }
  const collapsible = !context.preview && onCollapsedChange !== undefined;
  return (
    <SafePanel panelId={panel.id}>
      <div
        {...(context.preview
          ? { "data-panel-preview": panel.id }
          : { "data-panel-id": panel.id, "data-panel-type": panel.type })}
        className="h-full min-w-0"
      >
        <Panel
          title={panel.spec.title}
          icon={panelIcon(panel.spec.icon)}
          subtitle={rendered.state === "ready" ? rendered.subtitle : undefined}
          headingLevel={headingLevel}
          collapsible={collapsible}
          defaultCollapsed={collapsed}
          onCollapsedChange={onCollapsedChange}
          state={rendered.state === "empty" ? "empty" : "ready"}
          emptyTitle={rendered.state === "empty" ? rendered.title : undefined}
          emptyDescription={rendered.state === "empty" ? rendered.description : undefined}
          className="h-full"
        >
          {rendered.state === "ready" ? rendered.node : null}
        </Panel>
      </div>
    </SafePanel>
  );
}
