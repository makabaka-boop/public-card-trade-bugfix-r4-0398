import { useState } from "react";
import { Board } from "./Board";
import { Lobby } from "./Lobby";
import { loadSession, type Session } from "./api";
import { useDraftSocket } from "./useDraftSocket";

export default function App() {
  const [session, setSession] = useState<Session | null>(() => loadSession());
  const socket = useDraftSocket(session);

  if (!session) {
    return <Lobby onEnter={setSession} />;
  }
  return (
    <Board
      session={session}
      socket={socket}
      onLeave={() => setSession(null)}
    />
  );
}
