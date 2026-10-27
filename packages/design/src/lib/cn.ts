import { type ClassValue, clsx } from "clsx";
import { extendTailwindMerge } from "tailwind-merge";

/**
 * Сливает классы так, что последний побеждает. Роли `type-*` и слои `z-*` — наши утилиты,
 * поэтому tailwind-merge знает о них: `type-ui` не вытесняет цвет и не вытесняется им.
 */
const twMerge = extendTailwindMerge({
  extend: {
    classGroups: {
      "font-size": [
        { type: ["display", "title", "heading", "subheading", "body", "ui", "label", "caption", "mono"] },
      ],
      z: [{ z: ["sticky", "stage", "dock", "scrim", "layer", "toast", "banner"] }],
    },
  },
});

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
