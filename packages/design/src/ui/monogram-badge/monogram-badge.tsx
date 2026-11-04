import { cva, type VariantProps } from "class-variance-authority";
import type { CSSProperties } from "react";
import { selectAccent } from "../../lib/accents";
import { cn } from "../../lib/cn";
import { Icon } from "../icon";

const monogramVariants = cva(
  "inline-grid shrink-0 select-none place-items-center overflow-hidden whitespace-nowrap bg-accent text-center font-semibold text-fg-on-accent",
  {
    variants: {
      size: {
        sm: "size-8 rounded-md type-caption",
        md: "size-10 rounded-lg type-label",
        lg: "size-12 rounded-lg type-subheading",
      },
    },
    defaultVariants: { size: "md" },
  },
);

/** Знаков в монограмме не больше трёх: больше в квадрат 32 px при кегле 13 px не помещается. */
export const MONOGRAM_MAX_CHARS = 3;

const letters = (value: string) => Array.from(value);
const isLetterOrDigit = (char: string) => /[\p{L}\p{N}]/u.test(char);
const WORD_SEPARATORS = /[\s\-–—_/.,;:()«»"]+/u;

function pick(source: string): string {
  const words = source.split(WORD_SEPARATORS).filter((word) => letters(word).some(isLetterOrDigit));
  if (words.length === 0) return "";
  if (words.length === 1) {
    const chars = letters(words[0] as string);
    return (chars.length <= MONOGRAM_MAX_CHARS ? chars : chars.slice(0, 2)).join("").toUpperCase();
  }
  return words
    .map((word) => letters(word).find(isLetterOrDigit) ?? "")
    .slice(0, MONOGRAM_MAX_CHARS)
    .join("")
    .toUpperCase();
}

/**
 * Текст монограммы. Короткое название вуза (до трёх знаков) берётся как есть: «ВШЭ», «ЦУ». Длинное и полное
 * название сводится к первым буквам слов, не больше трёх: «Высшая школа экономики» → «ВШЭ». Одно длинное слово —
 * две первые буквы. Пустой результат — пустая строка (компонент покажет иконку вуза).
 */
export function monogramText(shortTitle: string | null | undefined, title: string): string {
  return pick(shortTitle?.trim() ?? "") || pick(title.trim());
}

export interface MonogramBadgeProps extends VariantProps<typeof monogramVariants> {
  /** Полное название вуза: имя для скринридера. */
  title: string;
  /** Короткое название из пространства вуза: «ВШЭ». Без него монограмма — первые буквы `title`. */
  shortTitle?: string | null;
  /** Тон акцента вуза из API (`accentHue`). Вне окна 235°…345° или `null` — акцент ВШЭ. */
  hue?: number | null;
  /** Знак рядом уже подписан названием вуза: скрыть от скринридера, чтобы не читать дважды. */
  decorative?: boolean;
  className?: string;
}

/**
 * Знак вуза: буквы на заливке его акцента. Логотипов нет: вуз узнаётся по буквам и цвету, а цвет никогда не
 * единственный носитель (буквы + имя для скринридера). Пара «fg-on-accent на accent» — AA в обеих темах для любого
 * тона окна (проверено тестом). Акцент применяется к самому знаку (`data-space` или переменные), соседей не красит.
 */
export function MonogramBadge({
  title,
  shortTitle,
  hue,
  size,
  decorative = false,
  className,
}: MonogramBadgeProps) {
  const accent = selectAccent(hue);
  const text = monogramText(shortTitle, title);
  const style = accent.kind === "custom" ? (accent.variables as CSSProperties) : undefined;
  const iconSize = size === "lg" ? 24 : size === "sm" ? 16 : 20;
  return (
    <span
      data-monogram=""
      data-space={accent.kind === "registry" ? accent.id : accent.kind === "default" ? "hse" : "custom"}
      style={style}
      {...(decorative ? { "aria-hidden": true } : { role: "img", "aria-label": title })}
      className={cn(monogramVariants({ size }), className)}
    >
      {text ? <span aria-hidden="true">{text}</span> : <Icon name="university" size={iconSize} />}
    </span>
  );
}
