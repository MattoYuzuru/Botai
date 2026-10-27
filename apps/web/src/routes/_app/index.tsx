import { createFileRoute } from "@tanstack/react-router";
import { TodayScreen } from "@/features/today/today-screen";

export const Route = createFileRoute("/_app/")({ component: TodayScreen });
