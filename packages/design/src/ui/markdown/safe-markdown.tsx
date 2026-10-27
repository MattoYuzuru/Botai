import { Fragment, type ReactNode } from "react";
import { cn } from "../../lib/cn";
import { displayHost, safeHttpUrl } from "../../lib/safe-url";
import { linkClass } from "../styles";
import { type Block, type Inline, parseMarkdown } from "./parse";

function renderInline(nodes: Inline[]): ReactNode {
  return nodes.map((node, index) => {
    // Разметка не меняет порядок: ключ по позиции стабилен для одного и того же текста.
    const key = `${node.type}-${index}`;
    switch (node.type) {
      case "text":
        return <Fragment key={key}>{renderLines(node.value)}</Fragment>;
      case "bold":
        return (
          <strong key={key} className="font-semibold">
            {renderInline(node.children)}
          </strong>
        );
      case "code":
        return (
          <code key={key} className="rounded-xs bg-bg-subtle px-1 type-mono text-fg">
            {node.value}
          </code>
        );
      case "link": {
        const url = safeHttpUrl(node.href);
        if (!url) return <Fragment key={key}>{renderInline(node.children)}</Fragment>;
        const label = node.children.map((child) => (child.type === "text" ? child.value : "")).join("");
        const host = displayHost(url);
        // Текст ссылки не должен скрывать, куда она ведёт: если в подписи нет хоста — показываем его рядом.
        const showHost = !label.includes(host);
        return (
          <Fragment key={key}>
            <a href={url.href} target="_blank" rel="noopener noreferrer nofollow" className={linkClass}>
              {renderInline(node.children)}
            </a>
            {showHost && <span className="type-caption text-fg-subtle"> ({host})</span>}
          </Fragment>
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

function renderBlock(block: Block, index: number, tail: ReactNode = null): ReactNode {
  switch (block.type) {
    case "paragraph":
      return (
        <p key={index}>
          {renderInline(block.children)}
          {tail}
        </p>
      );
    case "heading":
      return (
        <p key={index} className="type-subheading text-fg">
          {renderInline(block.children)}
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
              {renderInline(item)}
              {itemIndex === block.items.length - 1 ? tail : null}
            </li>
          ))}
        </List>
      );
    }
    case "quote":
      return (
        <blockquote key={index} className="border-l-2 border-border-strong pl-3 text-fg-muted">
          {renderInline(block.children)}
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
  }
}

export interface SafeMarkdownProps {
  source: string;
  className?: string;
  /** Курсор в конце текста: ответ ещё приходит. */
  caret?: boolean;
}

/**
 * Текст ответа агента. Без HTML и без ссылок-действий: React экранирует всё, что не разобрано,
 * ссылки — только http(s) в новой вкладке с `noopener`, картинки не загружаются.
 */
export function SafeMarkdown({ source, className, caret = false }: SafeMarkdownProps) {
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
      {blocks.map((block, index) => renderBlock(block, index, index === blocks.length - 1 ? cursor : null))}
      {caret && blocks.length === 0 && <p>{cursor}</p>}
    </div>
  );
}
