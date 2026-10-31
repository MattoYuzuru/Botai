import { Button, EmptyState } from "@botai/design";
import { Link, useNavigate, useRouter } from "@tanstack/react-router";
import { type ComponentType, useEffect } from "react";
import { Screen } from "@/app/screen";
import { ConnectPrompt } from "@/features/today/connect-prompt";
import { ListSkeleton } from "./parts";
import { RETIRED_STUDY_KINDS, SECTION_BY_KIND, type SectionDef, type StudyKind } from "./registry";
import type { SectionContext } from "./section-frame";
import { CoursesSection } from "./sections/courses";
import { GradesSection } from "./sections/grades";
import { LocationsSection } from "./sections/locations";
import { NewsSection } from "./sections/news";
import { PeopleSection } from "./sections/people";
import { useSectionScope } from "./use-section";

const BODIES: Record<StudyKind, ComponentType<{ ctx: SectionContext }>> = {
  courses: CoursesSection,
  grades: GradesSection,
  people: PeopleSection,
  locations: LocationsSection,
  news: NewsSection,
};

/**
 * Экран-раздел «Учёбы» (`/study/<вид>?space=<вуз>`): вид берётся из `SECTION_REGISTRY`, поэтому новый вид —
 * новая строка реестра и новый компонент списка, а не новая вкладка. Прежние места «Источники» и
 * «Документы и вложения» больше не экраны (D-013): их адреса ведут на «Учёбу», а не в пустоту.
 */
export function SectionScreen({ kind, space }: { kind: string; space?: string }) {
  const { directory, scope } = useSectionScope(space);
  const router = useRouter();
  const navigate = useNavigate();
  const def: SectionDef | undefined = SECTION_BY_KIND.get(kind);
  const retired = RETIRED_STUDY_KINDS.has(kind);
  useEffect(() => {
    if (retired) void navigate({ to: "/study", replace: true });
  }, [retired, navigate]);
  if (retired) return null;
  const back = () => {
    if (router.history.canGoBack()) router.history.back();
    else void navigate({ to: "/study" });
  };

  if (!def) {
    return (
      <Screen title="Раздел" leading="back" onBack={back}>
        <EmptyState
          icon="search"
          title="Такого раздела нет"
          description="Возможно, ссылка устарела."
          action={
            <Button variant="primary" asChild>
              <Link to="/study">К разделам</Link>
            </Button>
          }
        />
      </Screen>
    );
  }
  if (!directory.loaded) {
    return (
      <Screen title={def.title} leading="back" onBack={back}>
        <div data-screen={`study-${def.kind}`} data-state="loading">
          <ListSkeleton />
        </div>
      </Screen>
    );
  }
  if (directory.none) {
    return (
      <Screen title={def.title} leading="back" onBack={back}>
        <ConnectPrompt />
      </Screen>
    );
  }
  const Body = BODIES[def.kind];
  return <Body ctx={{ def, directory, scope }} />;
}
