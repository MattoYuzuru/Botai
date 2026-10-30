import { type CitationGroup, displayHost, type IconName, safeHttpUrl } from "@botai/design";
import { parseInternalLink } from "./internal-links";

/** Источник из блока `text` (`sources[]`). */
export interface TextSource {
  title: string;
  url: string;
  fetchedAt: string;
  stale?: boolean;
}

export interface ConnectionRef {
  id: string;
  title: string;
  origin: string;
  spaceTitle: string;
}

export interface ResolvedInternal {
  href: string;
  label: string;
  icon?: IconName;
  onOpen?: () => void;
}

const originOf = (url: string): string | null => {
  const safe = safeHttpUrl(url);
  return safe ? safe.origin : null;
};

/**
 * Цитаты ответа по источникам (F-11): одна запись на источник, подпись — название источника, а не адрес сайта.
 * Источник — подключение вуза: его узнают по `botai://connections/<id>` или по адресу страницы того же origin.
 * Документы и треды — каждый сам себе источник. Страница без подключения группируется по адресу сайта; название
 * ей даёт заголовок цитаты, а если заголовки разные — только адрес: выдумывать имя сайта нельзя.
 */
export function groupCitations(
  sources: readonly TextSource[],
  connections: readonly ConnectionRef[],
  resolve: (url: string) => ResolvedInternal | null,
): CitationGroup[] {
  const byId = new Map(connections.map((item) => [item.id, item]));
  const byOrigin = new Map<string, ConnectionRef>();
  for (const item of connections) {
    const origin = originOf(item.origin);
    if (origin && !byOrigin.has(origin)) byOrigin.set(origin, item);
  }

  interface Draft {
    key: string;
    label: string;
    host: string | null;
    titles: Set<string>;
    spaceTitle?: string;
    fetchedAt: number;
    stale: boolean;
    items: { id: string; title: string; url: string | null; internal?: ResolvedInternal }[];
    seen: Set<string>;
  }
  const drafts = new Map<string, Draft>();

  for (const source of sources) {
    const internal = parseInternalLink(source.url);
    const fetchedAt = Date.parse(source.fetchedAt);
    const safe = safeHttpUrl(source.url);
    const connection =
      internal?.kind === "connection" ? byId.get(internal.id) : safe ? byOrigin.get(safe.origin) : undefined;

    let key: string;
    let label: string;
    let host: string | null = null;
    if (connection) {
      key = `connection:${connection.id}`;
      label = connection.title;
    } else if (internal) {
      key = `${internal.kind}:${internal.id}`;
      label = source.title;
    } else if (safe) {
      key = `host:${safe.host}`;
      label = source.title;
      host = displayHost(safe);
    } else {
      key = `title:${source.title}`;
      label = source.title;
    }

    let draft = drafts.get(key);
    if (!draft) {
      draft = {
        key,
        label,
        host,
        titles: new Set(),
        spaceTitle: connection?.spaceTitle,
        fetchedAt: Number.isFinite(fetchedAt) ? fetchedAt : 0,
        stale: false,
        items: [],
        seen: new Set(),
      };
      drafts.set(key, draft);
    }
    draft.titles.add(source.title);
    if (Number.isFinite(fetchedAt)) draft.fetchedAt = Math.max(draft.fetchedAt, fetchedAt);
    if (source.stale) draft.stale = true;
    const dedupe = `${source.url}|${source.title}`;
    if (draft.seen.has(dedupe)) continue;
    draft.seen.add(dedupe);
    const resolved = internal ? resolve(source.url) : null;
    draft.items.push({
      id: `${key}#${draft.items.length}`,
      title: source.title,
      url: internal || safe ? source.url : null,
      internal: resolved ?? undefined,
    });
  }

  return [...drafts.values()].map((draft) => ({
    id: draft.key,
    // Страница без подключения с несколькими разными заголовками подписана адресом сайта, а не одним из заголовков.
    label:
      !draft.key.startsWith("host:") || draft.titles.size <= 1 ? draft.label : (draft.host ?? draft.label),
    spaceTitle: draft.spaceTitle,
    fetchedAt: draft.fetchedAt,
    stale: draft.stale || undefined,
    items: draft.items,
  }));
}
