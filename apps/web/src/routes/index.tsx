import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/")({
  component: Today,
});

function Today() {
  return (
    <main className="p-4">
      <h1 className="text-xl font-semibold">
        Bot<span className="text-accent-fg">AI</span>
      </h1>
    </main>
  );
}
