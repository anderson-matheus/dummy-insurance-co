import { relativeTime } from "../format";
import { S } from "../strings";
import { Conversation } from "../types";

interface Props {
  items: Conversation[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  error: boolean;
}

export function ConversationList({ items, selectedId, onSelect, onDelete, error }: Props) {
  return (
    <nav className="convlist" aria-label={S.conversations}>
      <h2 className="convlist__title">{S.conversations}</h2>
      {error && <p className="muted">{S.historyError}</p>}
      {items.length === 0 && !error && <p className="muted">{S.noConversations}</p>}
      <ul>
        {items.map((c) => (
          <li key={c.id} className={c.id === selectedId ? "convlist__item convlist__item--active" : "convlist__item"}>
            <button className="convlist__select" onClick={() => onSelect(c.id)} title={c.title ?? S.untitled}>
              <span className="convlist__name">{c.title ?? S.untitled}</span>
              <span className="convlist__meta">
                {relativeTime(c.updated_at)} · {c.message_count} msg
              </span>
            </button>
            <button className="convlist__delete" onClick={() => onDelete(c.id)} aria-label={S.deleteConversation} title={S.deleteConversation}>
              ×
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}
