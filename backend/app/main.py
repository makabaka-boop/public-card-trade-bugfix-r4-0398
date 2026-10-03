"""HTTP + WebSocket surface."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .config import settings
from .db import Repository
from .hub import Hub
from .service import GameError, GameService

_HTTP_STATUS = {
    "game_not_found": 404,
    "unauthorized": 401,
    "game_full": 409,
    "name_taken": 409,
    "not_enough_players": 409,
}


class CreateBody(BaseModel):
    rounds: Optional[int] = Field(default=None, ge=1, le=20)
    timeout: Optional[float] = Field(default=None, gt=0, le=3600)
    seed: Optional[int] = None


class JoinBody(BaseModel):
    name: str = Field(min_length=1, max_length=32)


def raise_game_error(exc: GameError) -> None:
    # Deliberately generic bodies: error responses never carry another
    # player's pack or pick data.
    raise HTTPException(status_code=_HTTP_STATUS.get(exc.code, 400), detail=exc.code)


def create_app(override_settings: "Settings | None" = None) -> FastAPI:
    used_settings = override_settings or settings
    repo = Repository(used_settings.db_path)
    hub = Hub()
    svc = GameService(repo, hub, used_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await repo.connect()
        await svc.recover_all()
        try:
            yield
        finally:
            await repo.close()

    app = FastAPI(title="Four-seat simulated card draft", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.repo = repo
    app.state.service = svc

    # ---- HTTP ----------------------------------------------------------
    @app.post("/games", status_code=201)
    async def create_game(body: CreateBody) -> Dict[str, Any]:
        game_id = await svc.create_game(
            num_rounds=body.rounds, timeout=body.timeout, seed=body.seed
        )
        return {"game_id": game_id}

    @app.post("/games/{game_id}/join", status_code=201)
    async def join_game(game_id: str, body: JoinBody) -> Dict[str, Any]:
        try:
            player_id, token, seat = await svc.join_game(game_id, body.name)
        except GameError as exc:
            raise_game_error(exc)
        # The token is returned exactly once; the server stores only a hash.
        return {
            "game_id": game_id,
            "player_id": player_id,
            "token": token,
            "seat": seat,
        }

    @app.post("/games/{game_id}/start")
    async def start_game(game_id: str) -> Dict[str, str]:
        try:
            await svc.start_game(game_id)
        except GameError as exc:
            raise_game_error(exc)
        return {"status": "started"}

    @app.get("/games/{game_id}")
    async def public_game(game_id: str, token: str = Query(...)) -> Dict[str, Any]:
        """Reconnect snapshot: projected for THIS player only."""
        try:
            player_id, _seat, _name = await svc.authenticate(game_id, token)
            return await svc.get_projection(game_id, player_id)
        except GameError as exc:
            raise_game_error(exc)


    @app.post('/games/{game_id}/trades')
    async def trade_action(game_id: str, body: dict, token: str = Query(...)):
        try:
            player_id, _seat, _name = await svc.authenticate(game_id,token)
            return await svc.trade_action(game_id,player_id,body)
        except GameError as exc:
            raise_game_error(exc)

    # ---- admin / deterministic-test hooks ------------------------------
    if settings.allow_admin:

        @app.post("/admin/games/{game_id}/timeout")
        async def admin_timeout(game_id: str) -> Dict[str, str]:
            try:
                await svc.force_timeout(game_id)
            except GameError as exc:
                raise_game_error(exc)
            return {"status": "resolved"}

        @app.get("/admin/games/{game_id}/state")
        async def admin_state(game_id: str) -> Dict[str, Any]:
            try:
                state = await svc.dump_state(game_id)
            except GameError as exc:
                raise_game_error(exc)
            return _full_state(state)

    # ---- WebSocket -----------------------------------------------------
    @app.websocket("/ws/{game_id}")
    async def ws_endpoint(
        websocket: WebSocket, game_id: str, token: str = Query(...)
    ) -> None:
        try:
            player_id, _seat, _name = await svc.authenticate(game_id, token)
        except GameError:
            # Rejected before any game data goes out (app-defined code).
            await websocket.close(code=4401)
            return

        await websocket.accept()
        hub.add(game_id, player_id, websocket)
        try:
            # Initial snapshot: full state internally, projected per seat by
            # the hub before it leaves the process.
            room = await svc.get_room(game_id)
            await hub.send_state(game_id, player_id, room.state)

            while True:
                text = await websocket.receive_text()
                try:
                    raw = json.loads(text)
                except (ValueError, TypeError):
                    await hub.send_error(
                        game_id, player_id, "invalid_message", websocket
                    )
                    continue
                await _handle_ws_message(
                    svc, hub, game_id, player_id, raw, websocket
                )
        except WebSocketDisconnect:
            pass
        finally:
            hub.remove(game_id, player_id, websocket)

    return app


async def _handle_ws_message(
    svc: GameService,
    hub: Hub,
    game_id: str,
    player_id: str,
    raw: Any,
    websocket: WebSocket,
) -> None:
    if not isinstance(raw, dict):
        await hub.send_error(game_id, player_id, "invalid_message", websocket)
        return
    mtype = raw.get("type")
    if mtype == "ping":
        await websocket.send_json({"type": "pong"})
    elif mtype == "submit_pick":
        await svc.submit_pick(game_id, player_id, raw.get("card_id"), websocket)
    else:
        await hub.send_error(game_id, player_id, "unknown_message_type", websocket)


def _full_state(state: Any) -> Dict[str, Any]:
    return {
        "game_id": state.game_id,
        "status": state.status,
        "seats": state.seats,
        "round_no": state.round_no,
        "deadline": state.deadline,
        "packs": {pid: list(cards) for pid, cards in state.packs.items()},
        "picks": {
            pid: {"card_id": c, "mode": m} for pid, (c, m) in state.picks.items()
        },
        "history": state.history,
        "version": state.version,
    }


app = create_app()
