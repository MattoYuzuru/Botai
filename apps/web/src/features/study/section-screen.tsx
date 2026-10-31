import { Button, EmptyState } from "@botai/design";
import { Link, useNavigate, useRouter } from "@tanstack/react-router";
import { type ComponentType, useEffect } from "react";
import { Screen } from "@/app/screen";
import { ConnectPrompt } from "@/features/today/connect-prompt";
import { ListSkeleton } from "./parts";
import { SECTION_BY_KIND, type SectionDef, type StudyKind } from "./registry";
import type { SectionContext } from "./section-frame";
import { ConnectionsSection } from "./sections/connections";
import { CoursesSection } from "./sections/courses";
import { DocumentsSection } from "./sections/documents";
import { GradesSection } from "./sections/grades";
import { LocationsSection } from "./sections/locations";
import { NewsSection } from "./sections/news";
import { PeopleSection } from "./sections/people";
import { useSectionScope } from "./use-section";
import { useStudyUiMode } from "./v4/flag";

const BODIES: Record<StudyKind, ComponentType<{ ctx: SectionContext }>> = {
  courses: CoursesSection,
  grades: GradesSection,
  people: PeopleSection,
  locations: LocationsSection,
  news: NewsSection,
  documents: DocumentsSection,
  connections: ConnectionsSection,
};

/**
 * Экран-раздел «Учёбы» (`/study/<вид>?space=<вуз>`): вид берётся из `SECTION_REGISTRY`, поэтому новый вид —
 * новая строка реестра и новый компонент списка, а не новая вкладка. Неизвестный вид — честный экран «Такого
 * раздела нет». Пока вузов нет — приглашение подключить.
 */
export function SectionScreen({ kind, space }: { kind: string; space?: string }) {
  const { directory, scope } = useSectionScope(space);
  const router = useRouter();
  const navigate = useNavigate();
  const def: SectionDef | undefined = SECTION_BY_KIND.get(kind);
  // В v4 «Источники» и «Документы и вложения» — не места навигации (03-sources-and-ux.md §5.5):
  // источники живут на экране вуза, документы — в его карточке. Прямая старая ссылка ведёт на «Учёбу».
  const v4 = useStudyUiMode() === "v4";
  const retired = v4 && (kind === "connections" || kind === "documents");
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
