/**
 * Вычисленный цвет токена в текущей теме (`rgb(...)` или `oklch(...)`): для canvas и витрины, где нельзя
 * написать `var(--color-…)` в стиле. Цвет берётся из вычисленного стиля пробного узла, поэтому следует теме
 * и акценту пространства вокруг `host`.
 */
export function resolveColorToken(token: string, host: HTMLElement = document.body): string {
  const probe = host.ownerDocument.createElement("span");
  probe.style.backgroundColor = `var(${token})`;
  probe.style.display = "none";
  host.appendChild(probe);
  const color = getComputedStyle(probe).backgroundColor;
  probe.remove();
  return color;
}
