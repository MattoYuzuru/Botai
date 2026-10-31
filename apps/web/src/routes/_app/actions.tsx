import { createFileRoute } from "@tanstack/react-router";
import { ActionsJournalScreen } from "@/features/actions/journal-screen";

/**
 * Журнал действий агента и решения по `ask` (docs/v4/19-actions.md). `?source=` открывает журнал,
 * отфильтрованный по источнику из его карточки.
 */
export const Route = createFileRoute("/_app/actions")({
  validateSearch: (search: Record<string, unknown>): { source?: string } =>
    typeof search.source === "string" && search.source ? { source: search.source } : {},
  component: function ActionsRoute() {
    const { source } = Route.useSearch();
    return <ActionsJournalScreen source={source} />;
  },
});
