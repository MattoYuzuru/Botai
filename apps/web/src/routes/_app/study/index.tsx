import { createFileRoute } from "@tanstack/react-router";
import { isStudyView, StudyScreenV4, type StudyView } from "@/features/study/v4/study-screen-v4";

/**
 * «Учёба»: успеваемость и источники вузов (03-sources-and-ux.md §5, D-013, D-046). Раздел — в `?view=`
 * (`grades` | `sources`); без него экран выбирает сам.
 */
export const Route = createFileRoute("/_app/study/")({
  validateSearch: (search: Record<string, unknown>): { view?: StudyView } =>
    isStudyView(search.view) ? { view: search.view } : {},
  component: function Study() {
    const { view } = Route.useSearch();
    return <StudyScreenV4 view={view} />;
  },
});
