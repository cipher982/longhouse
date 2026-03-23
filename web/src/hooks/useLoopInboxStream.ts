import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { connectLoopInboxStream, type LoopInboxItem } from "../services/api/oikos";

export interface UseLoopInboxStreamOptions {
  enabled?: boolean;
  selectedCardId?: number | null;
}

export function useLoopInboxStream(options: UseLoopInboxStreamOptions = {}) {
  const queryClient = useQueryClient();
  const enabled = options.enabled !== false;
  const selectedCardId = options.selectedCardId ?? null;

  useEffect(() => {
    if (!enabled || typeof EventSource === "undefined") {
      return;
    }

    return connectLoopInboxStream({
      onSnapshot: ({ items }) => {
        queryClient.setQueryData<LoopInboxItem[]>(["loop-inbox"], items);
        if (selectedCardId != null) {
          void queryClient.invalidateQueries({ queryKey: ["loop-action-card", selectedCardId] });
        }
      },
    });
  }, [enabled, queryClient, selectedCardId]);
}
