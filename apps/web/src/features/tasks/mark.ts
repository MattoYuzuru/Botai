import { toast } from "@botai/design";
import { type QueryClient, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, call } from "@/api/client";
import { humanMessage } from "@/api/errors";
import type { Deadline, Today } from "@/api/types";
import { asV3, type DeadlineUserState } from "@/api/v3-data";
import type { Fetched } from "@/features/data/fetch";

/** Что меняет отметка: отметить сданным, вернуть в работу или снять свою отметку. */
export interface MarkInput {
  id: string;
  userState: DeadlineUserState;
}

const DONE_TOAST: Record<Exclude<DeadlineUserState, null> | "clear", string> = {
  submitted: "Задание отмечено сданным",
  graded: "Задание отмечено проверенным",
  open: "Задание возвращено в работу",
  clear: "Отметка снята: статус снова из источника",
};

/** Кэши, где живёт задание: списки «Заданий» по вузам и «Сегодня». */
const DEADLINE_KEYS = [["deadlines"], ["today"]] as const;

function patchRow(row: Deadline, input: MarkInput): Deadline {
  if (row.id !== input.id) return row;
  // Снятие отметки: прежнее состояние источника знает только сервер — ждём его ответа.
  if (input.userState === null) return row;
  return { ...row, state: input.userState, stateSource: "student" };
}

function patchCaches(client: QueryClient, input: MarkInput) {
  client.setQueriesData<Fetched<Deadline[]>>({ queryKey: ["deadlines"] }, (data) =>
    data ? { ...data, data: data.data.map((row) => patchRow(row, input)) } : data,
  );
  client.setQueriesData<Fetched<Today>>({ queryKey: ["today"] }, (data) =>
    data
      ? { ...data, data: { ...data.data, deadlines: data.data.deadlines.map((row) => patchRow(row, input)) } }
      : data,
  );
}

/**
 * Отметка студента на задании (`PATCH /deadlines/{id}`, D-045): применяется сразу (оптимистично) во всех
 * списках, при ошибке возвращается прежнее и появляется тост; после ответа перечитываются задания, «Сегодня» и
 * «Новое».
 */
export function useMarkDeadline() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: MarkInput) =>
      call(
        asV3<Deadline>(
          api.PATCH("/api/v1/deadlines/{deadline_id}", {
            params: { path: { deadline_id: input.id } },
            body: { userState: input.userState },
          }),
        ),
      ),
    onMutate: async (input) => {
      await Promise.all(DEADLINE_KEYS.map((queryKey) => client.cancelQueries({ queryKey })));
      const snapshot = DEADLINE_KEYS.flatMap((queryKey) => client.getQueriesData({ queryKey }));
      patchCaches(client, input);
      return { snapshot };
    },
    onError: (error, _input, context) => {
      for (const [key, data] of context?.snapshot ?? []) client.setQueryData(key, data);
      toast("Не удалось отметить задание", { tone: "warning", description: humanMessage(error) });
    },
    onSuccess: (_data, input) => {
      toast(DONE_TOAST[input.userState ?? "clear"]);
    },
    onSettled: () =>
      Promise.all(
        [["deadlines"], ["today"], ["updates"]].map((queryKey) => client.invalidateQueries({ queryKey })),
      ),
  });
}
