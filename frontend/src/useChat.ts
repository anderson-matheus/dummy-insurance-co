import { useCallback, useEffect, useReducer, useRef } from "react";
import { api } from "./api";
import { initialState, reducer } from "./chatReducer";
import { ApiError, Message } from "./types";

const STALL_MS = 45_000; // above the backend deadline: a silent stream is treated as interrupted

function uuid(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`;
}

export function useChat(conversationId: string | null, onConversationChanged: () => void) {
  const [state, dispatch] = useReducer(reducer, initialState);
  const abortRef = useRef<AbortController | null>(null);
  const userCancelled = useRef(false);
  const stallTimer = useRef<number | null>(null);

  const loadHistory = useCallback(async (id: string) => {
    dispatch({ type: "HISTORY_LOADING" });
    try {
      const conv = await api.getConversation(id);
      dispatch({ type: "HISTORY_LOADED", messages: conv.messages });
    } catch {
      dispatch({ type: "HISTORY_FAILED" });
    }
  }, []);

  useEffect(() => {
    abortRef.current?.abort();
    dispatch({ type: "SELECT_CONVERSATION", id: conversationId });
    if (conversationId) void loadHistory(conversationId);
  }, [conversationId, loadHistory]);

  // a reply still pending on the server (e.g. opened in another tab): poll until it settles
  useEffect(() => {
    if (!conversationId || state.phase.kind !== "idle") return;
    if (!state.messages.some((m) => m.status === "pending")) return;
    const t = window.setTimeout(() => void loadHistory(conversationId), 2000);
    return () => window.clearTimeout(t);
  }, [conversationId, state.messages, state.phase.kind, loadHistory]);

  const clearStall = () => {
    if (stallTimer.current) window.clearTimeout(stallTimer.current);
    stallTimer.current = null;
  };
  const armStall = (controller: AbortController) => {
    clearStall();
    stallTimer.current = window.setTimeout(() => controller.abort(), STALL_MS);
  };

  const run = useCallback(
    async (content: string, clientMessageId: string, isRetry: boolean) => {
      if (!conversationId || state.phase.kind !== "idle") return;
      dispatch({ type: "SEND_START", content, clientMessageId, isRetry, now: Date.now() });
      const controller = new AbortController();
      abortRef.current = controller;
      userCancelled.current = false;
      let terminal = false;
      let assistantId: string | null = null;
      armStall(controller);
      try {
        await api.streamMessage(
          conversationId,
          { content, client_message_id: clientMessageId },
          (ev) => {
            armStall(controller);
            switch (ev.event) {
              case "accepted":
                assistantId = ev.data.assistant_message_id;
                dispatch({ type: "ACCEPTED", userMessageId: ev.data.user_message_id, assistantMessageId: ev.data.assistant_message_id, attempt: ev.data.attempt });
                break;
              case "stage":
                dispatch({ type: "STAGE", stage: ev.data.stage });
                break;
              case "sources":
                dispatch({ type: "SOURCES", sources: ev.data.sources });
                break;
              case "delta":
                dispatch({ type: "DELTA", text: ev.data.text });
                break;
              case "reset":
                dispatch({ type: "RESET_TEXT" });
                break;
              case "done":
                terminal = true;
                dispatch({ type: "DONE", message: ev.data.message });
                break;
              case "error":
                terminal = true;
                dispatch({ type: "STREAM_ERROR", message: ev.data.message as Message });
                break;
            }
          },
          controller.signal,
        );
        if (!terminal) dispatch({ type: "INTERRUPTED", reason: "stream_ended" });
      } catch (err) {
        if (terminal) return;
        if ((err as Error).name === "AbortError") {
          if (userCancelled.current) {
            dispatch({ type: "CANCELLED" });
            if (assistantId) void api.cancelMessage(conversationId, assistantId).catch(() => undefined);
          } else {
            dispatch({ type: "INTERRUPTED", reason: "stall" });
          }
        } else if (err instanceof ApiError) {
          dispatch({ type: "REQUEST_FAILED", code: err.code, message: err.message, retryable: err.retryable });
        } else {
          dispatch({ type: "INTERRUPTED", reason: "network" });
        }
      } finally {
        clearStall();
        abortRef.current = null;
        onConversationChanged();
      }
    },
    [conversationId, state.phase.kind, onConversationChanged],
  );

  const send = useCallback((content: string) => run(content, uuid(), false), [run]);

  const retry = useCallback(
    (assistantMessage: Message) => {
      const idx = state.messages.findIndex((m) => m.id === assistantMessage.id);
      const user = idx > 0 ? state.messages[idx - 1] : undefined;
      if (!user || user.role !== "user" || !user.client_message_id) return;
      void run(user.content, user.client_message_id, true);
    },
    [run, state.messages],
  );

  const cancel = useCallback(() => {
    userCancelled.current = true;
    abortRef.current?.abort();
  }, []);

  return { state, send, retry, cancel, reload: () => conversationId && loadHistory(conversationId) };
}
