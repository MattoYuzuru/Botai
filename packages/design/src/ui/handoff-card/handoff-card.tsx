import { useState } from "react";
import { cn } from "../../lib/cn";
import { displayHost, safeHttpUrl } from "../../lib/safe-url";
import { Button } from "../button/button";
import { Card } from "../card/card";
import { Icon } from "../icon";
import { toast } from "../toast/toast";

export interface HandoffCardProps {
  title: string;
  /** Страница вуза, которую человек откроет в новой вкладке. */
  url: string;
  /** Готовый текст или значения полей для копирования. */
  text: string;
  className?: string;
}

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Запасной путь для браузеров без Clipboard API или без разрешения.
    try {
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      const ok = document.execCommand("copy");
      area.remove();
      return ok;
    } catch {
      return false;
    }
  }
}

/**
 * Агент не может отправить форму сам (старая форма, дополнительная защита) — он отдаёт готовый текст:
 * «Скопировать» и ссылка «Открыть сайт вуза» в новой вкладке. Отправляет человек. Ссылка допустима только http(s).
 */
export function HandoffCard({ title, url, text, className }: HandoffCardProps) {
  const [copied, setCopied] = useState(false);
  const safe = safeHttpUrl(url);

  const copy = async () => {
    const ok = await copyText(text);
    setCopied(ok);
    if (ok) toast("Текст скопирован", { tone: "success", id: "handoff-copied" });
    else
      toast("Не удалось скопировать", {
        description: "Выделите текст и скопируйте вручную.",
        tone: "warning",
      });
  };

  return (
    <Card className={cn("flex flex-col gap-3", className)} data-handoff="">
      <h3 className="type-subheading text-fg">{title}</h3>
      <p className="type-caption text-fg-muted">
        Эту форму вуза я не могу отправить сам. Скопируйте текст и отправьте его на сайте вуза.
      </p>
      <pre className="max-h-60 whitespace-pre-wrap break-words rounded-md bg-bg-subtle p-3 type-body text-fg select-all">
        {text}
      </pre>
      <div className="flex flex-wrap gap-2">
        <Button variant="secondary" leadingIcon="copy" onClick={copy} className="min-w-fit flex-1">
          {copied ? "Скопировано" : "Скопировать"}
        </Button>
        {safe ? (
          <Button variant="primary" asChild className="min-w-fit flex-1">
            <a href={safe.href} target="_blank" rel="noopener noreferrer">
              Открыть сайт вуза
              <Icon name="external" size={20} />
              <span className="sr-only"> ({displayHost(safe)}), откроется в новой вкладке</span>
            </a>
          </Button>
        ) : (
          <p className="type-caption text-fg-muted">Адрес сайта вуза недоступен.</p>
        )}
      </div>
      {safe && <p className="type-caption text-fg-subtle">{displayHost(safe)}</p>}
    </Card>
  );
}
