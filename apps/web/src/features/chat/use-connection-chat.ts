import { toast } from "@botai/design";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { humanMessage } from "@/api/errors";
import { attachConnectionThread } from "./connection-api";
import { chatKeys } from "./queries";

/** Продолжает существующий разговор источника, не запускает новое подключение. */
export function useConnectionChat() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  return useMutation({
    mutationFn: (connectionId: string) => attachConnectionThread(connectionId),
    onSuccess: async (thread) => {
      await queryClient.invalidateQueries({ queryKey: chatKeys.threads });
      await navigate({ to: "/chat", search: { thread: thread.id } });
    },
    onError: (error) =>
      toast("Не удалось открыть разговор", { tone: "warning", description: humanMessage(error) }),
  });
}
