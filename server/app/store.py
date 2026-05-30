import json
import os
from pathlib import Path
from datetime import datetime
import aiofiles
from app.models import Workflow, BrowserSession, RunResult
from app.config import settings


class _Store:
    def __init__(self):
        base = Path(settings.DATA_DIR).resolve()
        self.workflows_dir = base / "workflows"
        self.sessions_dir = base / "sessions"
        self.profiles_dir = base / "browser_profiles"
        self.results_dir = base / "results"

    async def init(self):
        for d in (self.workflows_dir, self.sessions_dir, self.profiles_dir, self.results_dir):
            d.mkdir(parents=True, exist_ok=True)

    # --- Workflows ---

    async def save_workflow(self, wf: Workflow) -> None:
        path = self.workflows_dir / f"{wf.id}.json"
        async with aiofiles.open(path, "w") as f:
            await f.write(wf.model_dump_json(indent=2))

    async def load_workflow(self, wf_id: str) -> Workflow | None:
        path = self.workflows_dir / f"{wf_id}.json"
        try:
            async with aiofiles.open(path) as f:
                raw = await f.read()
            return Workflow.model_validate_json(raw)
        except FileNotFoundError:
            return None

    async def list_workflows(self) -> list[Workflow]:
        workflows = []
        for f in sorted(self.workflows_dir.glob("*.json")):
            try:
                raw = f.read_text()
                workflows.append(Workflow.model_validate_json(raw))
            except Exception:
                pass
        workflows.sort(key=lambda w: w.updated_at, reverse=True)
        return workflows

    async def delete_workflow(self, wf_id: str) -> bool:
        path = self.workflows_dir / f"{wf_id}.json"
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    async def get_latest_workflow(self) -> Workflow | None:
        wfs = await self.list_workflows()
        return wfs[0] if wfs else None

    # --- Sessions ---

    async def save_session(self, name: str, url: str = "", cookies: list[dict] | None = None) -> None:
        session = BrowserSession(
            name=name,
            url=url,
            cookies=cookies or [],
            created_at=datetime.now().isoformat(),
        )
        path = self.sessions_dir / f"{name}.json"
        async with aiofiles.open(path, "w") as f:
            await f.write(session.model_dump_json(indent=2))

    async def load_session(self, name: str) -> BrowserSession | None:
        path = self.sessions_dir / f"{name}.json"
        try:
            async with aiofiles.open(path) as f:
                raw = await f.read()
            return BrowserSession.model_validate_json(raw)
        except FileNotFoundError:
            return None

    async def list_sessions(self) -> list[BrowserSession]:
        sessions = []
        for f in self.sessions_dir.glob("*.json"):
            try:
                raw = f.read_text()
                sessions.append(BrowserSession.model_validate_json(raw))
            except Exception:
                pass
        return sessions

    async def delete_session(self, name: str) -> bool:
        deleted = False
        path = self.sessions_dir / f"{name}.json"
        try:
            path.unlink()
            deleted = True
        except FileNotFoundError:
            pass
        # Remove browser profile directory
        profile = self.profiles_dir / name
        if profile.is_dir():
            import shutil
            shutil.rmtree(profile, ignore_errors=True)
        return deleted

    # --- Run results (FR-6) ---

    async def save_result(self, result: RunResult) -> None:
        path = self.results_dir / f"{result.workflow_id}.json"
        async with aiofiles.open(path, "w") as f:
            await f.write(result.model_dump_json(indent=2))
        self.rotate_results()

    async def load_result(self, workflow_id: str) -> RunResult | None:
        path = self.results_dir / f"{workflow_id}.json"
        try:
            async with aiofiles.open(path) as f:
                raw = await f.read()
            return RunResult.model_validate_json(raw)
        except FileNotFoundError:
            return None

    async def list_results(self) -> list[RunResult]:
        results = []
        for f in self.results_dir.glob("*.json"):
            try:
                results.append(RunResult.model_validate_json(f.read_text()))
            except Exception:
                pass
        results.sort(key=lambda r: r.finished_at or r.started_at, reverse=True)
        return results

    def rotate_results(self) -> None:
        """Evict oldest result files by count and total size (FR-6)."""
        files = sorted(
            self.results_dir.glob("*.json"), key=lambda p: p.stat().st_mtime
        )

        # Cap by run count
        while len(files) > settings.RESULTS_MAX_RUNS:
            files.pop(0).unlink(missing_ok=True)

        # Cap by total size on disk
        max_bytes = settings.RESULTS_MAX_MB * 1024 * 1024
        total = sum(p.stat().st_size for p in files if p.exists())
        while files and total > max_bytes:
            oldest = files.pop(0)
            try:
                total -= oldest.stat().st_size
                oldest.unlink(missing_ok=True)
            except OSError:
                pass


STORE = _Store()
