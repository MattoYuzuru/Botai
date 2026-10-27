import { Wordmark } from "@botai/design";
import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/")({
  component: Today,
});

function Today() {
  return (
    <main className="px-safe-4 pt-safe-6 pb-safe-6 xs:px-safe-5">
      <h1 className="sr-only">BotAI</h1>
      <Wordmark size="lg" />
    </main>
  );
}
