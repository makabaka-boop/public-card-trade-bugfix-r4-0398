import { useState } from "react";
import {
  createGame,
  joinGame,
  saveSession,
  startGame,
  type Session,
} from "./api";

export function Lobby({
  onEnter,
}: {
  onEnter: (s: Session) => void;
}) {
  const [mode, setMode] = useState<"menu" | "create" | "join">("menu");
  const [gameId, setGameId] = useState("");
  const [name, setName] = useState("");
  const [rounds, setRounds] = useState(6);
  const [timeout, setTimeoutSecs] = useState(45);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleCreate() {
    setError(null);
    if (!name.trim()) return setError("请输入名字");
    setBusy(true);
    try {
      const gid = await createGame({ rounds, timeout });
      const joined = await joinGame(gid, name.trim());
      const session: Session = {
        gameId: gid,
        playerId: joined.player_id,
        token: joined.token,
        seat: joined.seat,
        name: name.trim(),
      };
      saveSession(session);
      // With 4 players this demo auto-starts right after the last join;
      // the host can also press start from the board while waiting.
      onEnter(session);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function handleJoin() {
    setError(null);
    if (!gameId.trim() || !name.trim())
      return setError("请填写房间号和名字");
    setBusy(true);
    try {
      const joined = await joinGame(gameId.trim(), name.trim());
      const session: Session = {
        gameId: gameId.trim(),
        playerId: joined.player_id,
        token: joined.token,
        seat: joined.seat,
        name: name.trim(),
      };
      saveSession(session);
      onEnter(session);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (mode === "menu") {
    return (
      <div className="card lobby">
        <h1>四人模拟卡牌轮抽</h1>
        <p className="muted">
          每轮一包五张，秘密选一张；四人都提交或时钟超时后同时公开，余牌传给下一人。
        </p>
        <div className="row">
          <button className="primary" onClick={() => setMode("create")}>
            创建房间
          </button>
          <button onClick={() => setMode("join")}>加入房间</button>
        </div>
        <p className="hint">
          提示：需要四人入座后由任意人点「开始游戏」。可用四个浏览器标签加入同一房间号测试。
        </p>
      </div>
    );
  }

  return (
    <div className="card lobby">
      <h1>{mode === "create" ? "创建房间" : "加入房间"}</h1>
      {mode === "join" && (
        <label>
          房间号
          <input
            value={gameId}
            onChange={(e) => setGameId(e.target.value)}
            placeholder="例如 ab12cd34ef56"
          />
        </label>
      )}
      <label>
        你的名字
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          maxLength={32}
          placeholder="座位上显示的名字"
        />
      </label>
      {mode === "create" && (
        <div className="grid2">
          <label>
            轮数
            <input
              type="number"
              min={1}
              max={20}
              value={rounds}
              onChange={(e) => setRounds(Number(e.target.value))}
            />
          </label>
          <label>
            每轮时限（秒）
            <input
              type="number"
              min={1}
              max={3600}
              value={timeout}
              onChange={(e) => setTimeoutSecs(Number(e.target.value))}
            />
          </label>
        </div>
      )}
      {error && <div className="error">{error}</div>}
      <div className="row">
        <button className="primary" disabled={busy} onClick={mode === "create" ? handleCreate : handleJoin}>
          {busy ? "请稍候…" : mode === "create" ? "创建并入座" : "加入"}
        </button>
        <button disabled={busy} onClick={() => setMode("menu")}>
          返回
        </button>
      </div>
    </div>
  );
}

export { startGame };
