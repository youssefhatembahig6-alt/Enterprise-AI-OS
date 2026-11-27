/**
 * Chat page state: conversations and their turns, kept in memory for the signed-in
 * session only (never written to storage; ADR 0101). Each turn is one independent
 * POST /api/v1/chat run: the backend receives no earlier turns.
 */

import type { AskMode } from "./reports";
import type { RunView } from "./runView";

export interface Turn {
  id: string;
  question: string;
  mode: AskMode;
  view: RunView;
  failure: string | null;
  running: boolean;
}

export interface Conversation {
  id: string;
  title: string;
  turns: Turn[];
}

export interface ChatState {
  conversations: Conversation[]; // newest first
  activeId: string | null; // null: the empty "new chat" screen
}

export const INITIAL_CHAT: ChatState = { conversations: [], activeId: null };

export type ChatAction =
  | { type: "new" }
  | { type: "select"; id: string }
  | { type: "ask"; conversationId: string; turnId: string; question: string; mode: AskMode; view: RunView }
  | { type: "view"; conversationId: string; turnId: string; view: RunView }
  | { type: "finish"; conversationId: string; turnId: string; failure: string | null };

export function titleFor(question: string): string {
  const flat = question.replace(/\s+/g, " ").trim();
  return flat.length > 48 ? `${flat.slice(0, 47)}…` : flat;
}

function updateTurn(
  state: ChatState,
  conversationId: string,
  turnId: string,
  change: (turn: Turn) => Turn,
): ChatState {
  return {
    ...state,
    conversations: state.conversations.map((c) =>
      c.id !== conversationId
        ? c
        : { ...c, turns: c.turns.map((t) => (t.id === turnId ? change(t) : t)) },
    ),
  };
}

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "new":
      return { ...state, activeId: null };
    case "select":
      return state.conversations.some((c) => c.id === action.id)
        ? { ...state, activeId: action.id }
        : state;
    case "ask": {
      const turn: Turn = {
        id: action.turnId,
        question: action.question,
        mode: action.mode,
        view: action.view,
        failure: null,
        running: true,
      };
      const existing = state.conversations.find((c) => c.id === action.conversationId);
      if (existing) {
        return {
          activeId: existing.id,
          conversations: state.conversations.map((c) =>
            c.id === existing.id ? { ...c, turns: [...c.turns, turn] } : c,
          ),
        };
      }
      const created: Conversation = {
        id: action.conversationId,
        title: titleFor(action.question),
        turns: [turn],
      };
      return { activeId: created.id, conversations: [created, ...state.conversations] };
    }
    case "view":
      return updateTurn(state, action.conversationId, action.turnId, (t) => ({ ...t, view: action.view }));
    case "finish":
      return updateTurn(state, action.conversationId, action.turnId, (t) => ({
        ...t,
        failure: action.failure,
        running: false,
      }));
  }
}

export function activeConversation(state: ChatState): Conversation | null {
  return state.conversations.find((c) => c.id === state.activeId) ?? null;
}
