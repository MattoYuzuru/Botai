import { createFileRoute } from "@tanstack/react-router";
import { StyleguidePage } from "@/features/styleguide/styleguide-page";

export const Route = createFileRoute("/styleguide")({
  component: StyleguidePage,
});
