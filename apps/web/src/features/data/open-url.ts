import { safeHttpUrl } from "@botai/design";
import type { Connection, Provenance } from "@/api/types";

type SourceAddress = Pick<Connection, "origin"> & Partial<Pick<Connection, "entryUrl">>;

/**
 * Адрес страницы источника для людей: «Открыть в личном кабинете» и цитата. Сначала `openUrl` записи (страница
 * интерфейса, где она видна), затем страница входа в данные источника, затем origin. Адрес API
 * (`provenance.sourceUrl`) — служебный провенанс и человеку не показывается и не открывается никогда.
 */
export function openUrlOf(
  record: { openUrl?: string | null } | null | undefined,
  provenance: Pick<Provenance, "openUrl">,
  source?: SourceAddress | null,
): string | null {
  for (const candidate of [record?.openUrl, provenance.openUrl, source?.entryUrl, source?.origin]) {
    const url = safeHttpUrl(candidate);
    if (url) return url.href;
  }
  return null;
}

/** Сайт источника: страница входа в данные либо origin. */
export function siteUrlOf(source: SourceAddress): string | null {
  return safeHttpUrl(source.entryUrl)?.href ?? safeHttpUrl(source.origin)?.href ?? null;
}
