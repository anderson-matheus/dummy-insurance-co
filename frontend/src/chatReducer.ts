import { Message, MessageStatus, SourceSummary } from "./types";

export type Phase =
  | { kind: "idle" }
  | { kind: "sending"; clientMessageId: string; startedAt: number }
  | { kind: "streaming"; clientMessageId: string; assistantId: string; stage: string; startedAt: number; sources: SourceSummary[] };

export interface ChatState {
  conversationId: string | null;
  historyStatus: "idle" | "loading" | "ready" | "error";
  messages: Message[];
  phase: Phase;
}

export type Action =
  | { type: "SELECT_CONVERSATION"; id: string | null }
  | { type: "HISTORY_LOADING" }
  | { type: "HISTORY_LOADED"; messages: Message[] }
  | { type: "HISTORY_FAILED" }
  | { type: "SEND_START"; content: string; clientMessageId: string; isRetry: boolean; now: number }
  | { type: "ACCEPTED"; userMessageId: string; assistantMessageId: string; attempt?: number }
  | { type: "STAGE"; stage: string }
  | { type: "SOURCES"; sources: SourceSummary[] }
  | { type: "DELTA"; text: string }
  | { type: "RESET_TEXT" }
  | { type: "DONE"; message: Message }
  | { type: "STREAM_ERROR"; message: Message }
  | { type: "CANCELLED" }
  | { type: "INTERRUPTED"; reason: string }
  | { type: "REQUEST_FAILED"; code: string; message: string; retryable: boolean };

export const initialState: ChatState = { conversationId: null, historyStatus: "idle", messages: [], phase: { kind: "idle" } };

function placeholder(id: string, status: MessageStatus = "pending"): Message {
  return { id, role: "assistant", content: "", status, attempt: 1, citations: [], created_at: new Date().toISOString() };
}

function currentAssistantIndex(state: ChatState): number {
  const p = state.phase;
  if (p.kind === "idle") return -1;
  const userIdx = state.messages.findIndex((m) => m.role === "user" && m.client_message_id === p.clientMessageId);
  if (userIdx === -1) return -1;
  const next = state.messages[userIdx + 1];
  return next && next.role === "assistant" ? userIdx + 1 : -1;
}

function updateAssistant(state: ChatState, patch: (m: Message) => Message): ChatState {
  const idx = currentAssistantIndex(state);
  if (idx === -1) return state;
  const messages = state.messages.slice();
  messages[idx] = patch(messages[idx]);
  return { ...state, messages };
}

export function reducer(state: ChatState, action: Action): ChatState {
  switch (action.type) {
    case "SELECT_CONVERSATION":
      return { ...initialState, conversationId: action.id };
    case "HISTORY_LOADING":
      return { ...state, historyStatus: "loading" };
    case "HISTORY_LOADED":
      return { ...state, historyStatus: "ready", messages: action.messages, phase: { kind: "idle" } };
    case "HISTORY_FAILED":
      return { ...state, historyStatus: "error" };
    case "SEND_START": {
      if (state.phase.kind !== "idle") return state;
      const phase: Phase = { kind: "sending", clientMessageId: action.clientMessageId, startedAt: action.now };
      if (action.isRetry) {
        // same client_message_id: reuse the user message and reset the failed reply in place
        const userIdx = state.messages.findIndex((m) => m.role === "user" && m.client_message_id === action.clientMessageId);
        if (userIdx === -1) return state;
        const messages = state.messages.slice();
        const existing = messages[userIdx + 1];
        const reply: Message = existing && existing.role === "assistant" ? { ...existing, content: "", status: "pending", error: null, citations: [], interruptionReason: undefined } : placeholder(`local-${action.clientMessageId}`);
        messages.splice(userIdx + 1, existing && existing.role === "assistant" ? 1 : 0, reply);
        return { ...state, messages, phase };
      }
      const user: Message = {
        id: `local-user-${action.clientMessageId}`,
        role: "user",
        content: action.content,
        status: "complete",
        attempt: 1,
        client_message_id: action.clientMessageId,
        citations: [],
        created_at: new Date().toISOString(),
      };
      return { ...state, messages: [...state.messages, user, placeholder(`local-${action.clientMessageId}`)], phase };
    }
    case "ACCEPTED": {
      if (state.phase.kind === "idle") return state;
      const { clientMessageId, startedAt } = state.phase;
      const withIds = updateAssistant(state, (m) => ({ ...m, id: action.assistantMessageId, attempt: action.attempt ?? m.attempt }));
      const messages = withIds.messages.map((m) => (m.role === "user" && m.client_message_id === clientMessageId ? { ...m, id: action.userMessageId } : m));
      return { ...withIds, messages, phase: { kind: "streaming", clientMessageId, assistantId: action.assistantMessageId, stage: "waiting", startedAt, sources: [] } };
    }
    case "STAGE":
      return state.phase.kind === "streaming" ? { ...state, phase: { ...state.phase, stage: action.stage } } : state;
    case "SOURCES":
      return state.phase.kind === "streaming" ? { ...state, phase: { ...state.phase, sources: action.sources } } : state;
    case "DELTA":
      return updateAssistant(state, (m) => ({ ...m, content: m.content + action.text }));
    case "RESET_TEXT":
      return updateAssistant(state, (m) => ({ ...m, content: "" }));
    case "DONE":
    case "STREAM_ERROR": {
      const next = updateAssistant(state, () => action.message);
      return { ...next, phase: { kind: "idle" } };
    }
    case "CANCELLED": {
      const next = updateAssistant(state, (m) => ({ ...m, status: "cancelled", error: { code: "CANCELLED", message: "Resposta cancelada pelo usuário.", retryable: true } }));
      return { ...next, phase: { kind: "idle" } };
    }
    case "INTERRUPTED": {
      const next = updateAssistant(state, (m) => ({ ...m, status: "interrupted", interruptionReason: action.reason, error: { code: "INTERRUPTED", message: "", retryable: true } }));
      return { ...next, phase: { kind: "idle" } };
    }
    case "REQUEST_FAILED": {
      const next = updateAssistant(state, (m) => ({ ...m, status: "error", error: { code: action.code, message: action.message, retryable: action.retryable } }));
      return { ...next, phase: { kind: "idle" } };
    }
    default:
      return state;
  }
}
