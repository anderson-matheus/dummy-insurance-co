import { useEffect, useRef, useState } from "react";
import { ChatState } from "../chatReducer";
import { S } from "../strings";
import { Message } from "../types";
import { MessageBubble } from "./MessageBubble";

interface Props {
  state: ChatState;
  onRetry: (m: Message) => void;
  onCancel: () => void;
  onReload: () => void;
}

export function ChatView({ state, onRetry, onCancel, onReload }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const [now, setNow] = useState(Date.now());
  const busy = state.phase.kind !== "idle";

  useEffect(() => {
    if (!busy) return;
    const t = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(t);
  }, [busy]);

  useEffect(() => {
    const el = listRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    if (nearBottom) bottomRef.current?.scrollIntoView({ block: "end" });
  }, [state.messages, state.phase]);

  if (!state.conversationId) return <div className="chat chat--empty"><p className="muted">{S.emptyChat}</p></div>;
  if (state.historyStatus === "loading") return <div className="chat chat--empty"><p className="muted">{S.loadingHistory}</p></div>;
  if (state.historyStatus === "error") {
    return (
      <div className="chat chat--empty">
        <p className="muted">{S.historyError}</p>
        <button className="btn" onClick={onReload}>{S.retry}</button>
      </div>
    );
  }

  const activeId = state.phase.kind === "streaming" ? state.phase.assistantId : state.phase.kind === "sending" ? `local-${state.phase.clientMessageId}` : null;

  return (
    <div className="chat" ref={listRef}>
      {state.messages.length === 0 && <p className="muted chat__hint">{S.emptyChat}</p>}
      {state.messages.map((m) => (
        <MessageBubble
          key={m.id}
          message={m}
          active={m.id === activeId || (state.phase.kind === "sending" && m.role === "assistant" && m.status === "pending" && m.id.startsWith("local-"))}
          phase={state.phase}
          now={now}
          onRetry={onRetry}
          onCancel={onCancel}
        />
      ))}
      <div ref={bottomRef} />
    </div>
  );
}
