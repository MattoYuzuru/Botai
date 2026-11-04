import {
  ChartBlock,
  DataTable,
  type DataTableColumn,
  ListRow,
  MonogramBadge,
  ProfileCard,
  RankList,
  StatGrid,
  StatTile,
} from "@botai/design";
import type { ReactNode } from "react";
import type { GradePointOut, PanelOut } from "@/api/panels";
import {
  chartGroups,
  dayText,
  formatNumber,
  gradeText,
  type PanelBody,
  pointTitle,
  profileView,
  rankItems,
  rankTotal,
  scaleOf,
  type TableColumnKey,
} from "./model";

/**
 * Реестр компонентов панелей (D-039): тип панели -> как рисовать её тело. Панель описывает сервер
 * (`type`, `spec.view`, `data`), в клиенте нет ничего, что зависело бы от вуза или источника. Новый тип —
 * новая строка здесь и в `model.readPanel`; неизвестный тип экран просто скрывает.
 */
export interface RenderContext {
  /** Пояс вуза: даты оценок показываются в нём. */
  zone: string;
  /** Предпросмотр предложения: без кнопок и ссылок внутри. */
  preview: boolean;
}

export type Rendered =
  | { state: "ready"; node: ReactNode; subtitle?: string }
  | { state: "empty"; title: string; description: string };

const NO_GRADES: Rendered = {
  state: "empty",
  title: "Оценок пока нет",
  description: "Они появятся, когда источник вуза отдаст оценки.",
};

type Body<K extends PanelBody["type"]> = Extract<PanelBody, { type: K }>;
type Renderers = {
  [K in PanelBody["type"]]: (body: Body<K>, panel: PanelOut, context: RenderContext) => Rendered;
};

/** Пустое значение источника — пустая ячейка: прочерков и заглушек экран не рисует. */
const cell = (value: string | null | undefined) => (value && value.trim() !== "" ? value : null);

function tileCaption(point: GradePointOut, zone: string): string {
  return [pointTitle(point), dayText(point.gradedAt, zone)].filter(Boolean).join(" · ");
}

function columnFor(key: TableColumnKey, zone: string, scaleInValue: boolean): DataTableColumn<GradePointOut> {
  switch (key) {
    case "title":
      return { key, header: "Работа", render: (point) => cell(point.title) };
    case "course":
      return { key, header: "Предмет", render: (point) => cell(point.course) };
    case "value":
      return {
        key,
        header: "Оценка",
        numeric: true,
        render: (point) => (scaleInValue ? gradeText(point) : point.value),
      };
    case "scale":
      return { key, header: "Шкала", render: (point) => cell(point.scale ?? scaleOf(point)) };
    case "date":
      return { key, header: "Дата", render: (point) => cell(dayText(point.gradedAt, zone)) };
  }
}

export const PANEL_RENDERERS: Renderers = {
  kpi: (body, _panel, { zone }) => {
    const data = body.data;
    if (!data || data.total === 0) return NO_GRADES;
    const tiles: ReactNode[] = [];
    for (const metric of body.metrics) {
      if (metric === "count") {
        tiles.push(
          <StatTile
            key="count"
            label="Оценок"
            value={formatNumber(data.total)}
            icon="grade"
            caption={data.numeric > 0 ? `с числом: ${formatNumber(data.numeric)}` : undefined}
          />,
        );
      } else {
        const point = metric === "latest" ? data.latest : data.best;
        if (!point) continue;
        tiles.push(
          <StatTile
            key={metric}
            label={metric === "latest" ? "Последняя оценка" : "Лучшая оценка"}
            value={point.value}
            scale={scaleOf(point)}
            icon={metric === "latest" ? "today" : "star"}
            caption={tileCaption(point, zone)}
          />,
        );
      }
    }
    if (tiles.length === 0) return NO_GRADES;
    return {
      state: "ready",
      node: (
        <StatGrid columns={tiles.length >= 3 ? 3 : 2} label="Показатели">
          {tiles}
        </StatGrid>
      ),
    };
  },

  chart: (body, panel, { zone, preview }) => {
    if (!body.data) return NO_GRADES;
    const groups = chartGroups(body.data, {
      variant: body.variant,
      x: body.x,
      title: panel.spec.title,
      zone,
    });
    if (groups.length === 0) {
      return {
        state: "empty",
        title: "Нечего показать на графике",
        description: "Нужны оценки, у которых источник назвал и число, и шкалу.",
      };
    }
    return {
      state: "ready",
      node: (
        <div className="flex flex-col gap-4">
          {groups.map((group) => (
            <div key={group.key} className="flex min-w-0 flex-col gap-1">
              <p className="type-caption text-fg-muted">Шкала источника: {group.scaleLabel}</p>
              <ChartBlock spec={group.spec} bare tableToggle={!preview} />
            </div>
          ))}
        </div>
      ),
    };
  },

  table: (body, panel, { zone }) => {
    if (!body.data || body.data.points.length === 0) return NO_GRADES;
    const scaleInValue = !body.columns.includes("scale");
    return {
      state: "ready",
      node: (
        <DataTable
          caption={panel.spec.title}
          captionHidden
          columns={body.columns.map((key) => columnFor(key, zone, scaleInValue))}
          rows={body.data.points}
          rowKey={(point) => point.id}
        />
      ),
    };
  },

  list: (body, panel, { zone }) => {
    if (!body.data || body.data.points.length === 0) return NO_GRADES;
    return {
      state: "ready",
      node: (
        <ul aria-label={panel.spec.title} className="-mx-3 flex flex-col">
          {body.data.points.map((point) => (
            <li key={point.id}>
              <ListRow
                title={pointTitle(point)}
                meta={
                  <>
                    {point.course && point.title && <span>{point.course}</span>}
                    {dayText(point.gradedAt, zone) && <span>{dayText(point.gradedAt, zone)}</span>}
                  </>
                }
                trailing={gradeText(point)}
              />
            </li>
          ))}
        </ul>
      ),
    };
  },

  ranking: (body, panel) => {
    const data = body.data;
    if (!data || data.rows.length === 0) {
      return {
        state: "empty",
        title: "Рейтинга пока нет",
        description: "Место появится, когда источник отдаст рейтинг.",
      };
    }
    return {
      state: "ready",
      subtitle: data.period ?? undefined,
      node: (
        <RankList
          label={panel.spec.title}
          items={rankItems(data, body.highlightMe)}
          total={rankTotal(data)}
        />
      ),
    };
  },

  profile: (body, panel) => {
    const data = body.data;
    if (!data || data.fields.length === 0) {
      return {
        state: "empty",
        title: "Профиль пока пуст",
        description: "Поля появятся, когда источник отдаст профиль.",
      };
    }
    const view = profileView(data);
    return {
      state: "ready",
      node: (
        <ProfileCard
          name={view.name ?? panel.space.title}
          subtitle={view.name ? panel.space.title : undefined}
          monogram={
            <MonogramBadge
              size="lg"
              title={panel.space.title}
              shortTitle={panel.space.shortTitle}
              hue={panel.space.accentHue}
              decorative
            />
          }
          fields={view.fields}
        />
      ),
    };
  },
};

export function renderPanelBody(body: PanelBody, panel: PanelOut, context: RenderContext): Rendered {
  const render = PANEL_RENDERERS[body.type] as (
    body: PanelBody,
    panel: PanelOut,
    context: RenderContext,
  ) => Rendered;
  return render(body, panel, context);
}
