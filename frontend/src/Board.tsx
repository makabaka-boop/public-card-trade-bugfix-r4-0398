import { TradePanel } from './TradePanel';
import { useState } from "react";
import { clearSession, startGame, type Session } from "./api";
import { useCountdown } from "./useCountdown";
import type { DraftSocket } from "./useDraftSocket";

export function Board({
  session,
  socket,
  onLeave,
}: {
  session: Session;
  socket: DraftSocket;
  onLeave: () => void;
}) {
  const { state, conn, lastError, submitPick } = socket;
  const [selected, setSelected] = useState<number | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const countdown = useCountdown(state?.deadline ?? null);

  if (!state) {
    return (
      <div className="card">
        <p>正在连接服务器…（{conn}）</p>
      </div>
    );
  }

  const seatName = (playerId: string, idx: number) =>
    playerId === session.playerId ? `${session.name}（你）` : `座位 ${idx + 1}`;

  async function handleStart() {
    setStartError(null);
    setStarting(true);
    try {
      await startGame(session.gameId);
    } catch (e) {
      setStartError((e as Error).message);
    } finally {
      setStarting(false);
    }
  }

  function confirmPick() {
    if (selected == null || state?.my_pick) return;
    submitPick(selected);
  }

  const waiting = state.status === "waiting" || state.round_no == null;
  const done = state.status === "completed";

  return (
    <div className="board">
      <header className="topbar">
        <div>
          <strong>房间 {state.game_id}</strong>{" "}
          <span className={`dot dot-${conn}`} title={conn} />
          <span className="muted"> {connLabel(conn)}</span>
        </div>
        <div>
          {session.name} · 座位 {session.seat + 1}
          <button
            className="link"
            onClick={() => {
              clearSession();
              onLeave();
            }}
          >
            离开
          </button>
        </div>
      </header>

      {waiting && (
        <div className="card center">
          <h2>等待开局</h2>
          <p>已入座 {state.seats.length} / 4</p>
          <ul className="seats">
            {[0, 1, 2, 3].map((i) => (
              <li key={i} className={state.seats[i] ? "filled" : "empty"}>
                {state.seats[i] ? seatName(state.seats[i].player_id, i) : `座位 ${i + 1}（空）`}
              </li>
            ))}
          </ul>
          <button className="primary" disabled={starting || state.seats.length !== 4} onClick={handleStart}>
            {starting ? "开始中…" : "开始游戏"}
          </button>
          {startError && <div className="error">{startError}</div>}
        </div>
      )}

      <TradePanel session={session} trades={((state as unknown as {trades: Parameters<typeof TradePanel>[0]["trades"]}).trades || [])} collections={state.collections} onRefresh={()=>{}} />
      {!waiting && (
        <>
          <div className="card status-bar">
            <div>
              第 <strong>{state.round_no}</strong> / {state.num_rounds} 轮
            </div>
            <div className={`timer ${countdown.expired ? "urgent" : ""}`}>
              {done ? "已结束" : `剩余 ${countdown.secondsLeft} 秒`}
            </div>
            <div className="locks">
              {state.seats.map((s, i) => {
                const locked = state.lock_state[s.player_id] === "locked";
                return (
                  <span key={s.player_id} className={`lock ${locked ? "locked" : "pending"}`}>
                    {seatName(s.player_id, i)}：{locked ? "✔ 已选" : "待选"}
                  </span>
                );
              })}
            </div>
          </div>

          <section className="card">
            <h2>你的手牌（仅你可见）</h2>
            {done ? (
              <p className="muted">本局已结束。</p>
            ) : state.my_pick ? (
              <p className="confirmed">
                你已秘密选择 <strong>Card-{String(state.my_pick.card_id).padStart(3, "0")}</strong>
                ，等待其他人…
              </p>
            ) : (
              <>
                <div className="hand">
                  {state.my_pack.map((c) => (
                    <button
                      key={c.id}
                      className={`card-tile ${selected === c.id ? "selected" : ""}`}
                      onClick={() => setSelected(c.id)}
                    >
                      <span className="card-name">{c.name}</span>
                    </button>
                  ))}
                </div>
                <div className="row">
                  <button
                    className="primary"
                    disabled={selected == null}
                    onClick={confirmPick}
                  >
                    {selected == null
                      ? "选择一张牌"
                      : `确认选择 Card-${String(selected).padStart(3, "0")}`}
                  </button>
                  {lastError && <span className="error">{errorText(lastError)}</span>}
                </div>
              </>
            )}
          </section>

          <section className="card">
            <h2>公开选择</h2>
            {state.reveals.length === 0 ? (
              <p className="muted">本轮选择尚未公开。</p>
            ) : (
              <table className="reveals">
                <thead>
                  <tr>
                    <th>轮次</th>
                    {state.seats.map((s, i) => (
                      <th key={s.player_id}>{seatName(s.player_id, i)}</th>
                    ))}
                    <th>方式</th>
                  </tr>
                </thead>
                <tbody>
                  {state.reveals.map((r) => (
                    <tr key={r.round_no}>
                      <td>#{r.round_no}</td>
                      {state.seats.map((s) => {
                        const p = r.picks[s.player_id];
                        return (
                          <td key={s.player_id} className={p.mode === "auto" ? "auto" : ""}>
                            {p.name}
                          </td>
                        );
                      })}
                      <td>{r.timed_out ? "⏱ 超时结算" : "全部提交"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          {done && (
            <section className="card">
              <h2>最终收集</h2>
              <ul className="collections">
                {state.seats.map((s, i) => (
                  <li key={s.player_id}>
                    {seatName(s.player_id, i)}：
                    {state.collections[s.player_id]
                      .map((c) => `Card-${String(c).padStart(3, "0")}`)
                      .join(", ")}
                  </li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}
    </div>
  );
}

function connLabel(c: string): string {
  return (
    { connecting: "连接中", open: "已连接", reconnecting: "断线重连中", closed: "已断开" } as Record<string, string>
  )[c] ?? c;
}

function errorText(code: string): string {
  return (
    {
      already_picked: "你本轮已经选过了",
      card_not_in_pack: "这张牌不在你的手牌中",
      round_advanced: "本轮已结算，选择无效",
      no_active_round: "当前没有进行中的轮次",
      invalid_card: "无效的卡牌",
      unknown_message_type: "未知操作",
    } as Record<string, string>
  )[code] ?? code;
}
