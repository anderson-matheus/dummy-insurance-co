import { useEffect, useState } from "react";
import { api } from "./api";
import { Health } from "./types";

export function useHealth() {
  const [health, setHealth] = useState<Health | null>(null);
  const [apiReachable, setApiReachable] = useState(true);
  const [checkedAt, setCheckedAt] = useState<number>(Date.now());

  useEffect(() => {
    let timer: number;
    let cancelled = false;
    const tick = async () => {
      try {
        const h = await api.health();
        if (cancelled) return;
        setHealth(h);
        setApiReachable(true);
      } catch {
        if (cancelled) return;
        setApiReachable(false);
      }
      setCheckedAt(Date.now());
      const degraded = !apiReachable || (health && health.status !== "ok");
      timer = window.setTimeout(tick, degraded ? 5000 : 15000);
    };
    void tick();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return { health, apiReachable, checkedAt };
}
