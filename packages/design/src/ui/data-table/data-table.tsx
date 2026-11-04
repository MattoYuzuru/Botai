import type { ReactNode } from "react";
import { cn } from "../../lib/cn";

export interface DataTableColumn<Row> {
  /** Ключ колонки: уникален в таблице. */
  key: string;
  /** Заголовок колонки. На телефоне становится подписью значения в карточке строки. */
  header: string;
  /** Числа и оценки: выравнивание по правому краю, табличные цифры. */
  numeric?: boolean;
  /** Значение ячейки. По умолчанию — `row[key]`. */
  render?: (row: Row) => ReactNode;
}

export interface DataTableProps<Row> {
  /** Название таблицы: подпись для скринридера и заголовок над таблицей, если `captionHidden` не задан. */
  caption: string;
  /** Не показывать подпись зрячим: заголовок уже дал `Panel`. Для скринридера она остаётся. */
  captionHidden?: boolean;
  /** Первая колонка — заголовок строки (`th scope="row"`): предмет, участник. */
  columns: readonly DataTableColumn<Row>[];
  rows: readonly Row[];
  /** Ключ строки. По умолчанию — индекс (строки статичны). */
  rowKey?: (row: Row, index: number) => string;
  /** Текст пустой таблицы. */
  empty?: ReactNode;
  className?: string;
}

/**
 * Роли таблицы заданы явно: при `display: block` на телефоне браузеры (Chrome, Safari) отбрасывают нативную
 * семантику таблицы. Объекты — чтобы линтер не считал роли избыточными: здесь они нужны.
 */
const ROLE = {
  table: { role: "table" },
  rowgroup: { role: "rowgroup" },
  row: { role: "row" },
  columnheader: { role: "columnheader" },
  rowheader: { role: "rowheader" },
  cell: { role: "cell" },
} as const;

function cellValue<Row>(column: DataTableColumn<Row>, row: Row): ReactNode {
  if (column.render) return column.render(row);
  const value = (row as Record<string, unknown>)[column.key];
  if (value === null || value === undefined) return null;
  return typeof value === "string" || typeof value === "number" ? value : String(value);
}

/**
 * Небольшая таблица. От 768 px — настоящая таблица (заголовки колонок, числа справа, первая колонка — заголовок строки).
 * На телефоне каждая строка — отдельная карточка: заголовок строки сверху, ниже пары «колонка — значение»; горизонтальной
 * прокрутки страницы нет. Роли ARIA заданы явно, потому что при смене `display` браузеры теряют семантику таблицы.
 * Таблица шире колонки на ноутбуке прокручивается только внутри своей области.
 */
export function DataTable<Row>({
  caption,
  captionHidden = false,
  columns,
  rows,
  rowKey,
  empty = "Строк нет",
  className,
}: DataTableProps<Row>) {
  const [first, ...rest] = columns;
  return (
    <div
      data-data-table=""
      className={cn("min-w-0 max-w-full md:overflow-x-auto", className)}
      // Область прокрутки на широком экране доступна с клавиатуры; на телефоне прокрутки нет.
      {...(rows.length > 0
        ? { role: "region", tabIndex: 0, "aria-label": `Прокручиваемая область: ${caption}` }
        : {})}
    >
      <table {...ROLE.table} className="block w-full border-collapse text-left type-ui md:table">
        <caption className={cn("text-left type-label text-fg", captionHidden ? "sr-only" : "block pb-2")}>
          {caption}
        </caption>
        <thead {...ROLE.rowgroup} className="max-md:sr-only md:table-header-group">
          <tr {...ROLE.row} className="md:table-row">
            {columns.map((column) => (
              <th
                key={column.key}
                {...ROLE.columnheader}
                scope="col"
                className={cn(
                  "px-3 py-2 type-label text-fg-muted md:border-b md:border-border md:bg-bg-subtle",
                  column.numeric ? "text-right" : "text-left",
                )}
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody {...ROLE.rowgroup} className="block md:table-row-group">
          {rows.length === 0 && (
            <tr {...ROLE.row} className="block md:table-row">
              <td
                {...ROLE.cell}
                colSpan={Math.max(1, columns.length)}
                className="block px-3 py-3 type-ui text-fg-muted md:table-cell"
              >
                {empty}
              </td>
            </tr>
          )}
          {rows.map((row, index) => (
            <tr
              key={rowKey ? rowKey(row, index) : index}
              {...ROLE.row}
              className={cn(
                "block rounded-md border border-border bg-surface px-3 py-2",
                "max-md:mb-2 max-md:last:mb-0",
                "md:table-row md:rounded-none md:border-0 md:border-b md:border-border md:bg-transparent md:p-0 md:last:border-b-0",
              )}
            >
              {first && (
                <th
                  scope="row"
                  {...ROLE.rowheader}
                  className={cn(
                    "block break-words py-1 text-left type-subheading text-fg md:table-cell md:px-3 md:py-2 md:font-normal",
                    first.numeric && "md:text-right",
                  )}
                >
                  {cellValue(first, row)}
                </th>
              )}
              {rest.map((column) => (
                <td
                  key={column.key}
                  {...ROLE.cell}
                  className={cn(
                    "flex items-baseline justify-between gap-3 py-1 text-fg md:table-cell md:px-3 md:py-2",
                    column.numeric ? "md:text-right" : "md:text-left",
                  )}
                  {...(column.numeric ? { "data-numeric": "" } : {})}
                >
                  <span aria-hidden="true" className="min-w-0 type-caption text-fg-muted md:hidden">
                    {column.header}
                  </span>
                  <span className={cn("min-w-0 break-words", column.numeric && "text-right")}>
                    {cellValue(column, row)}
                  </span>
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
