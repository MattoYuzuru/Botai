import { createFileRoute } from "@tanstack/react-router";
import { ScheduleScreen } from "@/features/schedule/schedule-screen";

export const Route = createFileRoute("/_app/schedule")({ component: ScheduleScreen });
