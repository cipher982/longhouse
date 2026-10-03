import { useQuery } from "@tanstack/react-query";
import { listMachineSummaries, listMachines } from "@/shared/api/index";

/** Directory alone: the nav status and the session readout share this cache entry. */
export function useMachineDirectory({ enabled = true, refetchInterval = 30_000 }: { enabled?: boolean; refetchInterval?: number | false } = {}) {
  return useQuery({
    queryKey: ["machine-directory"],
    queryFn: listMachines,
    enabled,
    refetchInterval,
    staleTime: 15_000,
    retry: false,
  });
}

/** Directory + activity + sync for the Machines page and a machine's page. */
export function useMachineSummaries() {
  return useQuery({
    queryKey: ["machine-summaries"],
    queryFn: () => listMachineSummaries(14),
    refetchInterval: 15_000,
    // One quick retry, then say so: a spinner that outlasts an outage reads as a hang.
    retry: 1,
    staleTime: 10_000,
  });
}
