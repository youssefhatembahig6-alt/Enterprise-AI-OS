import { useEffect, useState } from "react";
import { api, type Readiness } from "./api";

export type ApiStatus =
  | { kind: "checking" }
  | { kind: "unreachable" }
  | { kind: "degraded"; readiness: Readiness }
  | { kind: "ready"; readiness: Readiness };

/** Polls liveness and readiness so the UI can show whether the API is usable. */
export function useApiStatus(intervalMs = 15000): ApiStatus {
  const [status, setStatus] = useState<ApiStatus>({ kind: "checking" });

  useEffect(() => {
    let cancelled = false;
    const probe = async () => {
      try {
        await api.live();
        const readiness = await api.ready();
        if (!cancelled) {
          setStatus({ kind: readiness.status === "ready" ? "ready" : "degraded", readiness });
        }
      } catch {
        if (!cancelled) setStatus({ kind: "unreachable" });
      }
    };
    void probe();
    const timer = window.setInterval(() => void probe(), intervalMs);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [intervalMs]);

  return status;
}
