import { createFileRoute } from "@tanstack/react-router";
import { AppShell } from "@/app/app-shell";
import { requireSession } from "@/app/session";

/** Защищённая часть приложения: пять вкладок внутри каркаса. */
export const Route = createFileRoute("/_app")({
  beforeLoad: ({ context }) => requireSession(context.queryClient),
  component: AppShell,
});
