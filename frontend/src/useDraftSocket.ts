import { useCallback, useEffect, useRef, useState } from "react";
import {
  DraftStateView,
  ServerMessage,
  fetchState,
  wsUrl,
  type Session,
} from "./api";

export type ConnStatus = "connecting" | "open" | "reconnecting" | "closed";

export interface DraftSocket {
  state: DraftStateView | null;
  conn: ConnStatus;
  lastError: string | null;
  submitPick: (cardId: number) => void;
  /** Bumped whenever a pick_ack arrives, carrying the acknowledged card. */
  ack: { n: number; cardId: number | null };
}

/**
 * Owns the WebSocket lifecycle with automatic reconnect.
 *
 * Reconnect safety: on every (re)connect we take a fresh projected snapshot
 * from the server and then continue applying pushed `state` frames. Pending
 * picks are simply re-sent by the user; the server treats a repeat submit as
 * an idempotent ack, never a second pick.
 */
export function useDraftSocket(session: Session | null): DraftSocket {
  const [state, setState] = useState<DraftStateView | null>(null);
  const [conn, setConn] = useState<ConnStatus>("connecting");
  const [lastError, setLastError] = useState<string | null>(null);
  const [ack, setAck] = useState<{ n: number; cardId: number | null }>({
    n: 0,
    cardId: null,
  });
  const wsRef = useRef<WebSocket | null>(null);
  const retryRef = useRef<number | null>(null);
  const closedByUsRef = useRef(false);

  const connect = useCallback(
    async (sess: Session, isReconnect: boolean) => {
      setConn(isReconnect ? "reconnecting" : "connecting");
      try {
        // Snapshot first so a reconnect can never show stale local data.
        const snap = await fetchState(sess.gameId, sess.token);
        setState(snap);
      } catch {
        // Snapshot failure still warrants a socket attempt.
      }

      const ws = new WebSocket(wsUrl(sess.gameId, sess.token));
      wsRef.current = ws;

      ws.onopen = () => setConn("open");

      ws.onmessage = (ev) => {
        let msg: ServerMessage;
        try {
          msg = JSON.parse(ev.data as string) as ServerMessage;
        } catch {
          return;
        }
        if (msg.type === "state") {
          setState(msg.state);
        } else if (msg.type === "pick_ack") {
          setAck((a) => ({ n: a.n + 1, cardId: msg.card_id }));
        } else if (msg.type === "error") {
          setLastError(msg.code);
        }
      };

      ws.onclose = () => {
        if (closedByUsRef.current) {
          setConn("closed");
          return;
        }
        setConn("reconnecting");
        // Exponential-ish fixed retry; the game survives our absence.
        retryRef.current = window.setTimeout(() => {
          void connect(sess, true);
        }, 800);
      };

      ws.onerror = () => {
        ws.close();
      };
    },
    []
  );

  useEffect(() => {
    closedByUsRef.current = false;
    if (!session) {
      setState(null);
      setConn("closed");
      return;
    }
    void connect(session, false);
    return () => {
      closedByUsRef.current = true;
      if (retryRef.current) window.clearTimeout(retryRef.current);
      wsRef.current?.close();
    };
  }, [session, connect]);

  const submitPick = useCallback((cardId: number) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "submit_pick", card_id: cardId }));
    }
  }, []);

  return { state, conn, lastError, submitPick, ack };
}
