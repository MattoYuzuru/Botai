import { createFileRoute } from "@tanstack/react-router";
import { SectionScreen } from "@/features/study/section-screen";

/** Раздел «Учёбы»: вид — из `SECTION_REGISTRY`, вуз — в `?space=`. */
export const Route = createFileRoute("/_app/study/$kind")({
  validateSearch: (search: Record<string, unknown>): { space?: string } =>
    typeof search.space === "string" && search.space ? { space: search.space } : {},
  component: function StudySection() {
    const { kind } = Route.useParams();
    const { space } = Route.useSearch();
    return <SectionScreen kind={kind} space={space} />;
  },
});
