import { createFileRoute } from "@tanstack/react-router";
import { StudyScreen } from "@/features/study/study-screen";

export const Route = createFileRoute("/_app/study/")({ component: StudyScreen });
