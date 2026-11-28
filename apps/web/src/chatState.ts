/**
 * Chat page state (M8). Conversations are saved by the server for the signed-in employee
 * (GET/DELETE /api/v1/conversations); this reducer holds what the page shows: the Recents
 * list, the open conversation's turns and the answers streaming in. Nothing is written to
 * browser storage (ADR 0101). Each turn is one independent POST /api/v1/chat run: the
 * backend's orchestrator picks the handler and receives no earlier turns.
 */

import type { RunView } from "./runView";

export interface Turn {
  id: string;
  question: string;
  view: RunView;
  failure: string | null;
  running: boolean;
}

export interface Conversation {
  /** Page-local key: the server id for saved conversations, `local-…` until the first reply. */
  id: string;
  /** The server's conversation id, once known (X-Conversation-ID or the Recents list). */
  serverId: string | null;
  title: string;
  turns: Turn[];
  /** False for a Recents entry whose turns have not been fetched yet. */
  loaded: boolean;
}

export interface ChatState {
  conversations: Conversation[]; // most recently used first
  activeId: string | null; // null: the empty "new chat" screen
}

export const INITIAL_CHAT: ChatState = { conversations: [], activeId: null };

export interface SavedConversation {
  serverId: string;
  title: string;
}

export type ChatAction =
  | { type: "new" }
  | { type: "select"; id: string }
  | { type: "ask"; conversationId: string; turnId: string; question: string; view: RunView }
  | { type: "view"; conversationId: string; turnId: string; view: RunView }
  | { type: "finish"; conversationId: string; turnId: string; failure: string | null }
  | { type: "bind"; conversationId: string; serverId: string }
  | { type: "saved"; items: SavedConversation[] }
  | { type: "opened"; conversationId: string; turns: Turn[] }
  | { type: "removed"; conversationId: string };

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
        view: action.view,
        failure: null,
        running: true,
      };
      const existing = state.conversations.find((c) => c.id === action.conversationId);
      if (existing) {
        const moved = { ...existing, turns: [...existing.turns, turn] };
        return {
          activeId: existing.id,
          conversations: [moved, ...state.conversations.filter((c) => c.id !== existing.id)],
        };
      }
      const created: Conversation = {
        id: action.conversationId,
        serverId: null,
        title: titleFor(action.question),
        turns: [turn],
        loaded: true,
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
    case "bind":
      return {
        ...state,
        conversations: state.conversations.map((c) =>
          c.id === action.conversationId ? { ...c, serverId: action.serverId } : c,
        ),
      };
    case "saved": {
      // The server's list, merged with what this page already holds (a conversation that
      // is open or still waiting for its first reply keeps its turns).
      const known = new Map(state.conversations.filter((c) => c.serverId).map((c) => [c.serverId, c]));
      const ids = new Set(action.items.map((i) => i.serverId));
      // Not (yet) in the server's list: still waiting for its first reply, or saved after
      // the list was read.
      const pending = state.conversations.filter(
        (c) => !c.serverId || (c.loaded && c.turns.length > 0 && !ids.has(c.serverId)),
      );
      const listed = action.items.map(
        (item): Conversation =>
          known.get(item.serverId) ?? {
            id: item.serverId,
            serverId: item.serverId,
            title: item.title,
            turns: [],
            loaded: false,
          },
      );
      return { ...state, conversations: [...pending, ...listed] };
    }
    case "opened":
      return {
        ...state,
        conversations: state.conversations.map((c) =>
          c.id === action.conversationId ? { ...c, turns: action.turns, loaded: true } : c,
        ),
      };
    case "removed":
      return {
        activeId: state.activeId === action.conversationId ? null : state.activeId,
        conversations: state.conversations.filter((c) => c.id !== action.conversationId),
      };
  }
}

export function activeConversation(state: ChatState): Conversation | null {
  return state.conversations.find((c) => c.id === state.activeId) ?? null;
}
