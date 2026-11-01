import { NBSP, pluralRu } from "@botai/design";

/**
 * Деньги и токены в интерфейсе: числа берутся у сервера, клиент их не пересчитывает. Стоимость
 * приходит в долларах (по ней считает провайдер), малые суммы показываются четырьмя знаками.
 */
export function formatUsd(usd: number, locale = "ru-RU"): string {
  const fractionDigits = usd > 0 && usd < 0.01 ? 4 : 2;
  return new Intl.NumberFormat(locale, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  }).format(usd);
}

/** «12 345 токенов» — число с разрядами и неразрывным пробелом (styleguide §9). */
export function formatTokens(tokens: number): string {
  const grouped = new Intl.NumberFormat("ru-RU").format(tokens);
  return `${grouped}${NBSP}${pluralRu(tokens, ["токен", "токена", "токенов"])}`;
}
