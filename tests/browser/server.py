from __future__ import annotations

import uvicorn
from fakeredis.aioredis import FakeRedis

from a500_t_trader.signal_monitor import create_app
from a500_t_trader.strategy.intraday_v1_config import load_live_signal_config
from a500_t_trader.web_oidc import create_web_router


def authenticate(token: str) -> str:
    if token != "browser-test-access-token":
        raise ValueError("invalid test token")
    return "approved-user"


redis = FakeRedis(decode_responses=True)
app = create_app(
    redis=redis,
    config=load_live_signal_config(),
    authenticator=authenticate,
)
if not any(getattr(route, "path", None) == "/" for route in app.routes):
    app.include_router(create_web_router())


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
