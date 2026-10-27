import { createFileRoute } from "@tanstack/react-router";
import { TasksScreen } from "@/features/tasks/tasks-screen";

export const Route = createFileRoute("/_app/tasks")({ component: TasksScreen });
