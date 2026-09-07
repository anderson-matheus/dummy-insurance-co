import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { ChatView } from "./components/ChatView";
import { Composer } from "./components/Composer";
import { ConversationList } from "./components/ConversationList";
import { StatusBanner } from "./components/StatusBanner";
import { S } from "./strings";
import { Conversation } from "./types";
import { useChat } from "./useChat";
import { useHealth } from "./useHealth";

export default function App() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [listError, setListError] = useState(false);
  const { health, apiReachable, checkedAt } = useHealth();

  const refreshList = useCallback(async () => {
    try {
      const items = await api.listConversations();
      setConversations(items);
      setListError(false);
      return items;
    } catch {
      setListError(true);
      return [];
    }
  }, []);

  const chat = useChat(selectedId, () => void refreshList());

  useEffect(() => {
    void (async () => {
      const items = await refreshList();
      if (items.length > 0) setSelectedId(items[0].id);
    })();
  }, [refreshList]);

  const newConversation = async () => {
    try {
      const conv = await api.createConversation();
      setSelectedId(conv.id);
      await refreshList();
    } catch {
      setListError(true);
    }
  };

  const deleteConversation = async (id: string) => {
    if (!window.confirm(S.confirmDelete)) return;
    try {
      await api.deleteConversation(id);
    } catch {
      /* the list refresh below surfaces the state */
    }
    const items = await refreshList();
    if (selectedId === id) setSelectedId(items[0]?.id ?? null);
  };

  const handleSend = async (content: string) => {
    if (!selectedId) {
      const conv = await api.createConversation();
      setSelectedId(conv.id);
      await refreshList();
      // the chat hook re-binds to the new conversation on the next render; ask the user to resend
      return false;
    }
    await chat.send(content);
    return true;
  };

  const busy = chat.state.phase.kind !== "idle";
  const providerBlocked = !apiReachable;

  return (
    <div className="layout">
      <aside className="sidebar">
        <header className="sidebar__header">
          <h1>{S.appTitle}</h1>
          <button className="btn btn--primary" onClick={() => void newConversation()}>
            {S.newConversation}
          </button>
        </header>
        <ConversationList items={conversations} selectedId={selectedId} onSelect={setSelectedId} onDelete={(id) => void deleteConversation(id)} error={listError} />
      </aside>
      <main className="main">
        <StatusBanner health={health} apiReachable={apiReachable} checkedAt={checkedAt} />
        <ChatView state={chat.state} onRetry={chat.retry} onCancel={chat.cancel} onReload={() => void chat.reload()} />
        <Composer disabled={providerBlocked || !selectedId} busy={busy} onSend={handleSend} onCancel={chat.cancel} />
      </main>
    </div>
  );
}
