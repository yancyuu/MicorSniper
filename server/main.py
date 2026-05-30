import uvicorn
import webbrowser
from app.config import settings


def create_app():
    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.staticfiles import StaticFiles
    from app.routes import workflows, sessions, results
    from app.routes.sessions import session_stream_ws
    from app.ws.handlers import workflow_ws
    from app.store import STORE
    import os

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await STORE.init()
        yield

    app = FastAPI(lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # API routes
    app.include_router(workflows.router)
    app.include_router(sessions.router)
    app.include_router(results.router)

    # WebSocket routes
    app.websocket("/ws/workflows/{workflow_id}")(workflow_ws)
    app.websocket("/ws/sessions/{session_name}/stream")(session_stream_ws)

    # Serve frontend static files (production)
    dist_dir = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
    if os.path.isdir(dist_dir):
        app.mount("/", StaticFiles(directory=dist_dir, html=True), name="static")

    return app


if __name__ == "__main__":
    webbrowser.open(f"http://localhost:{settings.PORT}")
    uvicorn.run(
        "main:create_app",
        factory=True,
        host="0.0.0.0",
        port=settings.PORT,
        reload=settings.DEBUG,
    )
