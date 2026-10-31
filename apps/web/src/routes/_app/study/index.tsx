import { createFileRoute } from "@tanstack/react-router";
import { StudyScreenV4 } from "@/features/study/v4/study-screen-v4";

/** «Учёба»: вуз — карточка, его источники — живые записи (03-sources-and-ux.md §5, D-013). */
export const Route = createFileRoute("/_app/study/")({ component: StudyScreenV4 });
