/**
 * Сообщение агента с прогрессивной печатью. Живой блок (пришедший из снимка задания) печатается по code points;
 * исторический показывается целиком. Пока печать идёт, сообщение — `streaming`: в конце курсор, у статьи
 * `aria-busy`. Никакого своего `aria-live`: скринридер не должен читать каждый кадр.
 *
 * Клик по сообщению показывает всё сразу. Это вспомогательное действие, а не кнопка: сообщение остаётся
 * текстом, поэтому слушатель ставится нативно, без роли и клавиатурного обработчика.
 */
import { ChatMessage, type ChatMessageProps, useCalmMotion } from "@botai/design";
import { useEffect, useRef } from "react";
import { useTypewriter } from "./typing";

export type TypingMessageProps = Omit<ChatMessageProps, "role" | "markdown" | "status"> & {
  markdown: string;
  /** Блок пришёл живым из снимка задания: текст печатается. Решение фиксируется при монтировании. */
  animate?: boolean;
};

export function TypingMessage({ markdown, animate = false, ...message }: TypingMessageProps) {
  const calm = useCalmMotion();
  const typewriter = useTypewriter(markdown, animate, calm);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const element = root.current;
    if (!element || !typewriter.typing) return;
    const skip = () => typewriter.finish();
    element.addEventListener("click", skip);
    return () => element.removeEventListener("click", skip);
  }, [typewriter.typing, typewriter.finish]);

  return (
    <div ref={root} className="contents">
      <ChatMessage
        role="agent"
        markdown={typewriter.text}
        status={typewriter.typing ? "streaming" : "sent"}
        {...message}
      />
    </div>
  );
}
