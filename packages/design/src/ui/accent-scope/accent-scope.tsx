import type { CSSProperties, ReactNode } from "react";
import { selectAccent } from "../../lib/accents";

export interface AccentScopeProps {
  /** Тон акцента пространства из API (`accentHue`). Вне окна 235°…345° — акцент ВШЭ. */
  hue: number | null | undefined;
  /** Идентификатор «Все вузы» и прочие служебные значения сбрасывают акцент к ВШЭ. */
  children: ReactNode;
  className?: string;
  as?: "div" | "main";
}

/**
 * Применяет акцент пространства вуза к поддереву: id из реестра — атрибутом `data-space`, любой другой
 * допустимый тон — вычисленными переменными `--color-accent*`. Меняет только акцент; статусные цвета,
 * нейтрали и фокус от вуза не зависят.
 */
export function AccentScope({ hue, children, className, as: Tag = "div" }: AccentScopeProps) {
  const accent = selectAccent(hue);
  const style = accent.kind === "custom" ? (accent.variables as CSSProperties) : undefined;
  return (
    <Tag
      data-space={accent.kind === "registry" ? accent.id : accent.kind === "default" ? "hse" : "custom"}
      style={style}
      className={className}
    >
      {children}
    </Tag>
  );
}
