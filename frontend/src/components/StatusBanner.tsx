import { S } from "../strings";
import { Health } from "../types";

interface Props {
  health: Health | null;
  apiReachable: boolean;
  checkedAt: number;
}

export function StatusBanner({ health, apiReachable, checkedAt }: Props) {
  const ago = Math.max(0, Math.round((Date.now() - checkedAt) / 1000));
  if (!apiReachable) {
    return <div className="banner banner--danger" role="status">{S.banner.apiDown} <span className="muted">({S.banner.checked(ago)})</span></div>;
  }
  if (!health) return null;
  const p = health.provider;
  let text: string | null = null;
  if (!p.configured) text = S.banner.unconfigured;
  else if (p.circuit === "open") text = p.reason === "rate_limited" ? S.banner.rateLimited : S.banner.open;
  else if (p.circuit === "half_open") text = S.banner.halfOpen;
  if (!text) return null;
  return (
    <div className="banner banner--warn" role="status">
      {text} <span className="muted">({S.banner.checked(ago)})</span>
    </div>
  );
}
