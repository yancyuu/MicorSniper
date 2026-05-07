"""身份验证中间件 - 简化版"""
from typing import Optional
from sanic import Request, Sanic
from sanic.response import JSONResponse
from utils.logger import logger


class AuthMiddleware:
    """简化的身份验证中间件 - 直接验证 API Key"""

    def __init__(self, app: Sanic):
        self.app = app
        self.app.register_middleware(self.authenticate, "request")

    async def authenticate(
        self,
        request: Request,
        response: Optional[JSONResponse] = None
    ) -> Optional[JSONResponse]:
        if self._should_skip_auth(request):
            return None

        try:
            auth_header = request.headers.get('authorization')
            apikey = None

            if auth_header and auth_header.startswith('Bearer '):
                apikey = auth_header[7:]

            if not apikey:
                raise ValueError("缺少认证令牌")

            # 验证 API Key
            from config.settings import global_settings
            valid_key = global_settings.security.api_key
            if not valid_key:
                raise ValueError("服务端未配置 API_KEY")
            if apikey != valid_key:
                raise ValueError("无效的 API Key")

            # 存储到请求上下文
            request.ctx.auth_info = type('AuthInfo', (), {
                'source': type('Source', (), {'value': 'api'})(),
                'source_id': 'default',
            })()

            return None

        except ValueError as e:
            logger.warning(f"Auth failed: {request.method} {request.path} - {e}")
            return JSONResponse({"success": False, "error": "UNAUTHORIZED", "message": str(e)}, status=401)

    @staticmethod
    def _should_skip_auth(request: Request) -> bool:
        exempt_routes = ["/health", "/static", "/favicon.ico"]
        for route in exempt_routes:
            if request.path.startswith(route):
                return True
        if request.method == "OPTIONS":
            return True
        # 根路径也跳过（前端页面）
        if request.path == "/":
            return True
        return False
