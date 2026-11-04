import { toast } from "@botai/design";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { humanMessage } from "@/api/errors";
import {
  acceptPanel,
  dismissPanel,
  type PanelOut,
  type PanelPatch,
  panelKeys,
  panelsQuery,
  patchPanel,
} from "@/api/panels";
import { sourcesQuery } from "@/api/sources";

/** Панели: включённые и выключенные студентом (для настройки) и предложения агента. */
export function usePanelsData() {
  const active = useQuery(panelsQuery("active"));
  const proposed = useQuery(panelsQuery("proposed"));
  return {
    active: active.data ?? null,
    proposed: proposed.data ?? [],
    loading: active.isPending,
    error: active.isError ? active.error : null,
  };
}

type Lists = Record<"active" | "proposed", PanelOut[] | undefined>;

/**
 * Переключатели панели применяются сразу (как `Switch` в styleguide): список обновляется до ответа
 * сервера, при ошибке возвращается к прежнему состоянию и говорит об этом тостом.
 */
export function usePatchPanel() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: PanelPatch }) => patchPanel(id, patch),
    onMutate: async ({ id, patch }) => {
      await queryClient.cancelQueries({ queryKey: panelKeys.all });
      const previous = queryClient.getQueryData<PanelOut[]>(panelKeys.list("active"));
      queryClient.setQueryData<PanelOut[]>(panelKeys.list("active"), (rows) =>
        rows?.map((row) =>
          row.id === id
            ? {
                ...row,
                visible: patch.visible ?? row.visible,
                hiddenInAll: patch.hiddenInAll ?? row.hiddenInAll,
                position: patch.position ?? row.position,
              }
            : row,
        ),
      );
      return { previous };
    },
    onError: (error, _vars, context) => {
      if (context?.previous) queryClient.setQueryData(panelKeys.list("active"), context.previous);
      toast("Не удалось изменить панель", { tone: "error", description: humanMessage(error) });
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: panelKeys.all }),
  });
}

/** Принять или отклонить предложение агента: карточка исчезает сразу, при ошибке возвращается. */
export function useDecideProposal(action: "accept" | "dismiss") {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => (action === "accept" ? acceptPanel(id) : dismissPanel(id)),
    onMutate: async (id) => {
      await queryClient.cancelQueries({ queryKey: panelKeys.all });
      const previous: Lists = {
        active: queryClient.getQueryData<PanelOut[]>(panelKeys.list("active")),
        proposed: queryClient.getQueryData<PanelOut[]>(panelKeys.list("proposed")),
      };
      queryClient.setQueryData<PanelOut[]>(panelKeys.list("proposed"), (rows) =>
        rows?.filter((row) => row.id !== id),
      );
      return { previous };
    },
    onSuccess: (panel) => {
      if (action === "accept") toast(`Панель «${panel.spec.title}» добавлена`, { tone: "success" });
    },
    onError: (error, _id, context) => {
      if (context?.previous.proposed)
        queryClient.setQueryData(panelKeys.list("proposed"), context.previous.proposed);
      toast(action === "accept" ? "Не удалось добавить панель" : "Не удалось отклонить предложение", {
        tone: "error",
        description: humanMessage(error),
      });
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: panelKeys.all }),
  });
}

/**
 * Данные панелей меняются, когда источник вуза закончил синхронизацию. Узнаём это по списку источников,
 * который экран и так обновляет в фоне: сменилась отметка последней синхронизации или статус — панели
 * перечитываются. Первый ответ не считается событием.
 */
export function useRefreshPanelsOnSync(spaceIds: readonly string[]) {
  const queryClient = useQueryClient();
  const signature = useQueries({
    queries: spaceIds.map((id) => sourcesQuery(id)),
    combine: (results) =>
      results
        .map((result) =>
          (result.data ?? [])
            .map((source) => `${source.id}:${source.status}:${source.lastSyncAt ?? ""}`)
            .join(","),
        )
        .join("|"),
  });
  const previous = useRef<string | null>(null);
  useEffect(() => {
    if (signature === "" || signature.replaceAll("|", "") === "") return;
    if (previous.current !== null && previous.current !== signature) {
      void queryClient.invalidateQueries({ queryKey: panelKeys.all });
    }
    previous.current = signature;
  }, [signature, queryClient]);
}
