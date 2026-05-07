"""自定义异常类"""


class BusinessException(Exception):
    def __init__(self, message: str, details: dict = None):
        self.message = message
        self.details = details or {}
        super().__init__(self.message)


class ContextNotFoundException(BusinessException):
    def __init__(self, message: str = "登录态不存在，请先登录"):
        super().__init__(message=message, details={"error_type": "context_not_found"})


class SessionCreationException(BusinessException):
    def __init__(self, message: str = "创建浏览器会话失败"):
        super().__init__(message=message, details={"error_type": "session_creation_failed"})


class BrowserInitializationException(BusinessException):
    def __init__(self, message: str = "浏览器初始化失败"):
        super().__init__(message=message, details={"error_type": "browser_init_failed"})
