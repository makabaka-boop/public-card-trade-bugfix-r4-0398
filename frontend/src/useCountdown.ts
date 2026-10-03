import { useEffect, useState } from "react";

interface Countdown {
  secondsLeft: number;
  expired: boolean;
}

/** Ticks once a second against the server-provided absolute deadline. */
export function useCountdown(deadline: number | null): Countdown {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (deadline == null) return;
    const id = window.setInterval(() => setNow(Date.now() / 1000), 250);
    return () => window.clearInterval(id);
  }, [deadline]);
  if (deadline == null) return { secondsLeft: 0, expired: false };
  const secondsLeft = Math.max(0, Math.ceil(deadline - now));
  return { secondsLeft, expired: secondsLeft === 0 };
}
