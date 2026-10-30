import { Fragment, type MouseEvent, type ReactNode } from "react";
import { cn } from "../../lib/cn";
import { displayHost, safeAppHref, safeHttpUrl } from "../../lib/safe-url";
import { linkClass } from "../styles";
import { ChartBlock } from "./chart-block";
import { type Block, type Inline, parseMarkdown, type TableAlign } from "./parse";

/** Внутренняя ссылка приложения (`botai://threads/…`): куда вести и как открыть без перезагрузки. */
export interface ResolvedLink {
  /** Адрес внутри приложения (`/chat?thread=…`) или абсолютный http(s). Другие значения отбрасываются. */
  href: string;
  /** Подпись для голого адреса вместо `botai://…`. */
  label?: string;
  /** Открыть без перезагрузки страницы (обычный щелчок); Ctrl/Cmd/средняя кнопка идут как у обычной ссылки. */
  onOpen?: () => void;
}

export type LinkResolver = (url: string) => ResolvedLink | null;

const openInApp = (onOpen: (() => void) | undefined) => (event: MouseEvent<HTMLAnchorElement>) => {
  if (!onOpen || event.defaultPrevented || event.button !== 0) return;
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  onOpen();
};

/** Ключ позиции: ячейки и строки разобранного текста статичны и не переупорядочиваются. */
const slot = (...parts: number[]) => parts.join("-");

const plainText = (nodes: Inline[]): string =>
  nodes
    .map((node) =>
      node.type === "text" || node.type === "code"
        ? node.value
        : "children" in node
          ? plainText(node.children)
          : "",
    )
    .join("");

function renderInline(nodes: Inline[], resolveLink?: LinkResolver): ReactNode {
  return nodes.map((node, index) => {
    // Разметка не меняет порядок: ключ по позиции стабилен для одного и того же текста.
    const key = `${node.type}-${index}`;
    switch (node.type) {
      case "text":
        return <Fragment key={key}>{renderLines(node.value)}</Fragment>;
      case "bold":
        return (
          <strong key={key} className="font-semibold">
            {renderInline(node.children, resolveLink)}
          </strong>
        );
      case "italic":
        return (
          <em key={key} className="italic">
            {renderInline(node.children, resolveLink)}
          </em>
        );
      case "code":
        return (
          <code key={key} className="rounded-xs bg-bg-subtle px-1 type-mono text-fg">
            {node.value}
          </code>
        );
      case "link": {
        const url = safeHttpUrl(node.href);
        if (url) {
          const label = plainText(node.children);
          const host = displayHost(url);
          // Текст ссылки не должен скрывать, куда она ведёт: если в подписи нет хоста — показываем его рядом.
          const showHost = !label.includes(host);
          return (
            <Fragment key={key}>
              <a href={url.href} target="_blank" rel="noopener noreferrer nofollow" className={linkClass}>
                {renderInline(node.children, resolveLink)}
              </a>
              {showHost && <span className="type-caption text-fg-subtle"> ({host})</span>}
            </Fragment>
          );
        }
        const target = resolveLink?.(node.href) ?? null;
        const href = target ? safeAppHref(target.href) : null;
        if (!target || !href)
          return <Fragment key={key}>{renderInline(node.children, resolveLink)}</Fragment>;
        // Голый адрес: подпись берём у резолвера, а не показываем `botai://…`.
        const bare = target.label !== undefined && plainText(node.children) === node.href;
        return (
          <a key={key} href={href} onClick={openInApp(target.onOpen)} data-internal="" className={linkClass}>
            {bare ? target.label : renderInline(node.children, resolveLink)}
          </a>
        );
      }
      default:
        return null;
    }
  });
}

function renderLines(value: string): ReactNode {
  const lines = value.split("\n");
  return lines.map((line, index) => (
    // biome-ignore lint/suspicious/noArrayIndexKey: строки одного текста не переупорядочиваются
    <Fragment key={index}>
      {index > 0 && <br />}
      {line}
    </Fragment>
  ));
}

const ALIGN: Record<NonNullable<TableAlign>, string> = {
  left: "text-left",
  center: "text-center",
  right: "text-right tabular-nums",
};

function renderTable(block: Extract<Block, { type: "table" }>, resolveLink?: LinkResolver): ReactNode {
  return (
    // Прокручиваемая область фокусируема, чтобы широкую таблицу можно было листать с клавиатуры.
    <section
      // biome-ignore lint/a11y/noNoninteractiveTabindex: широкую таблицу листают с клавиатуры
      tabIndex={0}
      aria-label="Таблица"
      data-md-table=""
      className="max-w-full overflow-x-auto rounded-md border border-border"
    >
      <table className="w-full border-collapse type-ui [overflow-wrap:normal]">
        <thead className="bg-bg-subtle">
          <tr>
            {block.header.map((cell, column) => (
              <th
                key={slot(column)}
                scope="col"
                className={cn(
                  "min-w-28 px-3 py-2 align-bottom font-semibold text-fg",
                  ALIGN[block.align[column] ?? "left"],
                )}
              >
                {renderInline(cell, resolveLink)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {block.rows.map((row, rowIndex) => (
            <tr key={slot(rowIndex)} className="border-t border-border">
              {row.map((cell, column) => (
                <td
                  key={slot(column)}
                  className={cn("min-w-28 px-3 py-2 align-top", ALIGN[block.align[column] ?? "left"])}
                >
                  {renderInline(cell, resolveLink)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function renderBlock(
  block: Block,
  index: number,
  resolveLink?: LinkResolver,
  tail: ReactNode = null,
): ReactNode {
  switch (block.type) {
    case "paragraph":
      return (
        <p key={index}>
          {renderInline(block.children, resolveLink)}
          {tail}
        </p>
      );
    case "heading":
      return (
        <p key={index} className="type-subheading text-fg">
          {renderInline(block.children, resolveLink)}
          {tail}
        </p>
      );
    case "list": {
      const List = block.ordered ? "ol" : "ul";
      return (
        <List
          key={index}
          className={cn("flex flex-col gap-1 pl-5", block.ordered ? "list-decimal" : "list-disc")}
        >
          {block.items.map((item, itemIndex) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: пункты списка не переупорядочиваются
            <li key={itemIndex}>
              {renderInline(item, resolveLink)}
              {itemIndex === block.items.length - 1 ? tail : null}
            </li>
          ))}
        </List>
      );
    }
    case "quote":
      return (
        <blockquote key={index} className="border-l-2 border-border-strong pl-3 text-fg-muted">
          {renderInline(block.children, resolveLink)}
          {tail}
        </blockquote>
      );
    case "code":
      return (
        <pre key={index} className="overflow-x-auto rounded-md bg-bg-subtle p-3 type-mono text-fg">
          <code>{block.value}</code>
          {tail}
        </pre>
      );
    case "table":
      return (
        <Fragment key={index}>
          {renderTable(block, resolveLink)}
          {tail && <p>{tail}</p>}
        </Fragment>
      );
    case "chart":
      return (
        <Fragment key={index}>
          <ChartBlock spec={block.spec} />
          {tail && <p>{tail}</p>}
        </Fragment>
      );
  }
}

export interface SafeMarkdownProps {
  source: string;
  className?: string;
  /** Курсор в конце текста: ответ ещё приходит. */
  caret?: boolean;
  /**
   * Внутренние ссылки (`botai://threads/…`, `botai://attachments/…`): спрашивается для каждого абсолютного адреса не
   * http(s). `null` — не ссылка, остаётся текст. Возвращённый `href` проверяется: путь приложения или http(s).
   */
  resolveLink?: LinkResolver;
}

/**
 * Текст ответа агента. Без HTML и без ссылок-действий: React экранирует всё, что не разобрано,
 * внешние ссылки — только http(s) в новой вкладке с `noopener`, внутренние — через `resolveLink`, картинки не
 * загружаются. Таблицы GFM прокручиваются по горизонтали, fenced-блок ```chart рисуется графиком.
 */
export function SafeMarkdown({ source, className, caret = false, resolveLink }: SafeMarkdownProps) {
  const blocks = parseMarkdown(source);
  const cursor = caret ? (
    <span
      aria-hidden="true"
      data-caret=""
      className="ml-0.5 inline-block h-[1em] w-0.5 translate-y-0.5 animate-caret bg-fg-muted align-baseline"
    />
  ) : null;
  return (
    <div data-safe-markdown="" className={cn("flex flex-col gap-3 type-body text-fg", className)}>
      {blocks.map((block, index) =>
        renderBlock(block, index, resolveLink, index === blocks.length - 1 ? cursor : null),
      )}
      {caret && blocks.length === 0 && <p>{cursor}</p>}
    </div>
  );
}
