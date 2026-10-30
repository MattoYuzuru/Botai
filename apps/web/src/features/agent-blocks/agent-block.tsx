import { ChatMessage, HandoffCard, PlanCard, WaitCard } from "@botai/design";
import type { ParsedBlock } from "@/features/jobs/blocks";
import { RemoteScreen } from "@/features/remote-screen/remote-screen";
import { viewerFromBlock } from "./attachments";
import { ChoiceCard } from "./choice-card";
import type { BlockContext } from "./context";
import { InputCard } from "./input-card";
import { ApprovalBlock, ConfirmCard, SummaryCard, UnknownBlockView } from "./other-cards";
import { TaskBlockView } from "./task-block";

/**
 * Один блок интерфейса агента (ADR-0004) — только компонентами дизайн-системы. Клиент ничего не исполняет
 * из блока: текст — безопасный markdown, ссылки — http(s), неизвестный тип — текстом с пометкой.
 * `thinking` здесь не рисуется: шаги собирает `ThinkingStream`.
 */
export function AgentBlockView({ block, ctx }: { block: ParsedBlock; ctx: BlockContext }) {
  if (ctx.readOnly) {
    switch (block.type) {
      case "input":
      case "choice":
        return <ChatMessage role="agent" markdown={block.question} />;
      case "confirm":
        return (
          <ChatMessage
            role="agent"
            markdown={block.details ? `${block.details}\n\n${block.question}` : block.question}
          />
        );
      case "approval":
        return <ChatMessage role="agent" markdown={block.proposal.intent} />;
      case "login":
        return <ChatMessage role="agent" markdown="Вход через браузер" />;
      case "wait":
        return <ChatMessage role="agent" markdown={block.message} />;
    }
  }
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
          confidence={ctx.confidence?.(block.markdown)}
          citationGroups={block.sources ? ctx.groupSources?.(block.sources) : undefined}
          resolveLink={ctx.resolveLink}
        />
      );
    case "plan":
      return <PlanCard title={block.title} steps={block.steps} />;
    case "attachment":
      return (
        <ChatMessage
          role="agent"
          attachments={[viewerFromBlock(block)]}
          onOpenAttachment={ctx.openAttachment}
          at={Date.parse(block.at)}
          nowMs={ctx.nowMs}
          timeZone={ctx.timeZone}
        />
      );
    case "task":
      return <TaskBlockView block={block} ctx={ctx} />;
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
