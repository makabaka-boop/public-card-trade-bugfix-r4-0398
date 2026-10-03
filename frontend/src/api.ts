// Shared shape of the per-player projection the server pushes.
// Important: this deliberately has NO field for other players' current
// packs. The server only ever sends the viewer's own `my_pack`.

export type LockState = "pending" | "locked";

export interface CardView {
  id: number;
  name: string;
}

export interface PublicPick {
  card_id: number;
  name: string;
  mode: "manual" | "auto";
}

export interface Reveal {
  round_no: number;
  timed_out: boolean;
  picks: Record<string, PublicPick>;
}

export interface Seat {
  player_id: string;
  seat: number;
}

export interface TradeOffer {
  from: string;
  to: string;
  card_id: number;
}

export interface TradeView {
  id: string;
  revision: number;
  author: string;
  offers: TradeOffer[];
  participants: string[];
  confirmed: string[];
  status: "open" | "committed" | "cancelled";
}

export interface DraftStateView {
  status: "waiting" | "active" | "completed";
  game_id: string;
  you: string;
  seats: Seat[];
  round_no: number | null;
  num_rounds: number;
  deadline: number | null;
  pack_size: number;
  my_pack: CardView[];
  my_pick: { card_id: number; mode: "manual" | "auto" } | null;
  lock_state: Record<string, LockState>;
  reveals: Reveal[];
  trades: TradeView[];
  collections: Record<string, number[]>;
  version: number;
}

export interface JoinResult {
  game_id: string;
  player_id: string;
  token: string;
  seat: number;
}

export type ServerMessage =
  | { type: "state"; state: DraftStateView }
  | { type: "pick_ack"; card_id: number }
  | { type: "error"; code: string }
  | { type: "pong" };

const JSON_HEADERS = { "Content-Type": "application/json" };

export async function createGame(body: {
  rounds?: number;
  timeout?: number;
  seed?: number;
}): Promise<string> {
  const res = await fetch("/games", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error((await res.json()).error);
  return (await res.json()).game_id;
}

export async function joinGame(gameId: string, name: string): Promise<JoinResult> {
  const res = await fetch(`/games/${gameId}/join`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ name }),
  });
  if (!res.ok) throw new Error((await res.json()).error);
  return res.json();
}

export async function startGame(gameId: string): Promise<void> {
  const res = await fetch(`/games/${gameId}/start`, { method: "POST" });
  if (!res.ok) throw new Error((await res.json()).error);
}

export async function fetchState(gameId: string, token: string): Promise<DraftStateView> {
  const res = await fetch(`/games/${gameId}?token=${encodeURIComponent(token)}`);
  if (!res.ok) throw new Error((await res.json()).error);
  return res.json();
}

export function wsUrl(gameId: string, token: string): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws/${gameId}?token=${encodeURIComponent(token)}`;
}

const SESSION_KEY = "draft.session";

export interface Session {
  gameId: string;
  playerId: string;
  token: string;
  seat: number;
  name: string;
}

export function saveSession(s: Session): void {
  localStorage.setItem(SESSION_KEY, JSON.stringify(s));
}

export function loadSession(): Session | null {
  const raw = localStorage.getItem(SESSION_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as Session;
  } catch {
    return null;
  }
}

export function clearSession(): void {
  localStorage.removeItem(SESSION_KEY);
}
