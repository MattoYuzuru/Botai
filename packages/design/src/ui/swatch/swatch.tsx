import { useLayoutEffect, useRef, useState } from "react";
import { cn } from "../../lib/cn";
import { contrastRatio, parseCssColor, toHex } from "../../lib/color";

export interface SwatchProps {
  /** Имя CSS-переменной: `--palette-hse-600` или `--color-accent`. */
  token: string;
  /** Подпись; по умолчанию — имя токена без `--`. */
  label?: string;
  /** Токен фона, к которому считается контраст (по умолчанию `--color-bg`). */
  against?: string;
  className?: string;
}

function readColor(token: string, host: HTMLElement) {
  const probe = host.ownerDocument.createElement("span");
  probe.style.backgroundColor = `var(${token})`;
  probe.style.display = "none";
  host.appendChild(probe);
  const color = parseCssColor(getComputedStyle(probe).backgroundColor);
  probe.remove();
  return color;
}

/**
 * Образец цвета для витрины /styleguide: hex и контраст считаются из вычисленного цвета токена
 * в текущей теме, а не хранятся руками, поэтому витрина не расходится с CSS.
 */
export function Swatch({ token, label, against = "--color-bg", className }: SwatchProps) {
  const ref = useRef<HTMLDivElement>(null);
  const [info, setInfo] = useState<{ hex: string; ratio: number | null } | null>(null);

  useLayoutEffect(() => {
    const host = ref.current;
    if (!host) return;
    const read = () => {
      const color = readColor(token, host);
      const base = readColor(against, host);
      if (!color) return setInfo(null);
      setInfo({ hex: toHex(color), ratio: base && color.alpha === 1 ? contrastRatio(color, base) : null });
    };
    read();
    // Тема меняется атрибутом на <html> или на предке витрины: перечитываем.
    const observer = new MutationObserver(read);
    observer.observe(host.ownerDocument.documentElement, {
      attributes: true,
      subtree: true,
      attributeFilter: ["data-theme", "data-space"],
    });
    return () => observer.disconnect();
  }, [token, against]);

  return (
    <div ref={ref} className={cn("flex flex-col gap-1", className)}>
      <div
        className="h-12 rounded-md border border-border"
        style={{ backgroundColor: `var(${token})` }}
        aria-hidden="true"
      />
      <div className="type-caption text-fg">{label ?? token.replace(/^--(color-|palette-)?/, "")}</div>
      <div className="type-mono text-fg-muted">
        {info?.hex ?? " "}
        {info?.ratio ? ` · ${info.ratio.toFixed(1)}` : ""}
      </div>
    </div>
  );
}
