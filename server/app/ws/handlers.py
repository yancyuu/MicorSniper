from fastapi import WebSocket, WebSocketDisconnect
import json


class ConnectionManager:
    def __init__(self):
        self._clients: dict[str, set[WebSocket]] = {}

    def add_client(self, workflow_id: str, ws: WebSocket):
        if workflow_id not in self._clients:
            self._clients[workflow_id] = set()
        self._clients[workflow_id].add(ws)

    def remove_client(self, workflow_id: str, ws: WebSocket):
        clients = self._clients.get(workflow_id)
        if clients:
            clients.discard(ws)
            if not clients:
                del self._clients[workflow_id]

    async def broadcast(self, workflow_id: str, message: dict):
        clients = self._clients.get(workflow_id, set())
        dead = set()
        for ws in clients:
            try:
                await ws.send_json(message)
            except Exception:
                dead.add(ws)
        clients -= dead


manager = ConnectionManager()


async def workflow_ws(websocket: WebSocket, workflow_id: str):
    await websocket.accept()
    manager.add_client(workflow_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        manager.remove_client(workflow_id, websocket)
