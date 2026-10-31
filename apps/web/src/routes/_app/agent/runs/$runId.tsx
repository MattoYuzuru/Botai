import { createFileRoute } from "@tanstack/react-router";
import { AgentRunTraceScreen } from "@/features/agent-runs/trace-screen";

/**
 * Трасса хода агента v4 по id из карточки задачи или плашки работы (`/agent/runs/<run_id>`).
 * Ссылки не ходят на служебные адреса API: экран читает `/api/v1/agent/runs/{id}` и его шаги.
 */
export const Route = createFileRoute("/_app/agent/runs/$runId")({
  component: function AgentRunRoute() {
    const { runId } = Route.useParams();
    return <AgentRunTraceScreen runId={runId} />;
  },
});
