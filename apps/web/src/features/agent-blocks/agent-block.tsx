import { ChatMessage, HandoffCard, WaitCard } from "@botai/design";
import type { ParsedBlock } from "@/features/jobs/blocks";
import { RemoteScreen } from "@/features/remote-screen/remote-screen";
import { ChoiceCard } from "./choice-card";
import type { BlockContext } from "./context";
import { InputCard } from "./input-card";
import { ApprovalBlock, ConfirmCard, SummaryCard, UnknownBlockView } from "./other-cards";

/**
 * Один блок интерфейса агента (ADR-0004) — только компонентами дизайн-системы. Клиент ничего не исполняет
 * из блока: текст — безопасный markdown, ссылки — http(s), неизвестный тип — текстом с пометкой.
 * `thinking` здесь не рисуется: шаги собирает `ThinkingStream`.
 */
export function AgentBlockView({ block, ctx }: { block: ParsedBlock; ctx: BlockContext }) {
  switch (block.type) {
    case "thinking":
      return null;
    case "text":
      return (
        <ChatMessage
          role="agent"
          markdown={block.markdown}
          at={Date.parse(block.at)}
          nowMs={ctx.nowMs}
          timeZone={ctx.timeZone}
          citations={block.sources?.map((source, index) => ({
            id: `${block.id}-${index}`,
            title: source.title,
            url: source.url,
            fetchedAt: Date.parse(source.fetchedAt),
            stale: source.stale,
            spaceTitle: ctx.spaceTitle,
          }))}
        />
      );
    case "choice":
      return <ChoiceCard block={block} ctx={ctx} />;
    case "input":
      return <InputCard block={block} ctx={ctx} />;
    case "confirm":
      return <ConfirmCard block={block} ctx={ctx} />;
    case "summary":
      return <SummaryCard block={block} ctx={ctx} />;
    case "approval":
      return <ApprovalBlock block={block} ctx={ctx} />;
    case "wait":
      return (
        <WaitCard
          message={block.message}
          expiresAt={block.expiresAt ? Date.parse(block.expiresAt) : undefined}
          nowMs={ctx.nowMs}
        />
      );
    case "handoff":
      return <HandoffCard title={block.title} url={block.url} text={block.text} />;
    case "login":
      return <RemoteScreen block={block} nowMs={ctx.nowMs} />;
    case "unknown":
      return <UnknownBlockView block={block} />;
  }
}
