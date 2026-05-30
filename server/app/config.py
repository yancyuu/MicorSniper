from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    PORT: int = 1111
    DEBUG: bool = True
    DATA_DIR: str = "../data"
    LLM_PROVIDER: str = "openai/gpt-4o-mini"
    LLM_BASE_URL: str = ""
    OPENAI_API_KEY: str = ""
    # Run-result retention (FR-6 rotation)
    RESULTS_MAX_RUNS: int = 200
    RESULTS_MAX_MB: int = 500

    # Proxy — static URL (e.g. http://user:pass@host:port)
    PROXY_URL: str = ""
    # Proxy — KuaiDaili API (auto-fetch, takes priority over PROXY_URL)
    KDL_SECRET_ID: str = ""
    KDL_SECRET_KEY: str = ""

    model_config = {"env_file": ".env", "env_prefix": ""}


settings = Settings()
