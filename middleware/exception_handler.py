"""异常处理中间件"""
import traceback
from sanic import Sanic
from sanic.request import Request
from sanic.response import HTTPResponse
from sanic.exceptions import NotFound
from utils.logger import logger


class ExceptionHandlerMiddleware:
    def __init__(self, app: Sanic):
        self.app = app
        self.setup_exception_handlers(app)

    def setup_exception_handlers(self, app: Sanic):
        @app.exception(NotFound)
        async def not_found_handler(request: Request, exc: NotFound) -> HTTPResponse:
            from sanic.response import json
            return json({"success": False, "error": "Not found"}, status=404)

        @app.exception(Exception)
        async def global_exception_handler(request: Request, exc: Exception):
            from sanic.response import json
            logger.error(f"Exception: {exc}\n{traceback.format_exc()}")
            return json({"success": False, "error": str(exc)}, status=500)
