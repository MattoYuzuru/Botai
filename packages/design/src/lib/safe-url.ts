/**
 * Внешняя ссылка допустима, только если это абсолютный http(s)-адрес. Схемы javascript:, data:, blob:,
 * file:, mailto:, intent: и относительные пути — не ссылки: клиент никогда ничего не исполняет из блока агента.
 */
export function safeHttpUrl(value: string | null | undefined): URL | null {
  if (!value) return null;
  try {
    const url = new URL(value.trim());
    if (url.protocol !== "https:" && url.protocol !== "http:") return null;
    if (!url.hostname) return null;
    return url;
  } catch {
    return null;
  }
}

/** Хост без «www.» — для подписи ссылки, чтобы человек видел, куда ведёт текст. */
export function displayHost(url: URL): string {
  return url.hostname.replace(/^www\./, "");
}
