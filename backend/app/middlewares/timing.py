from __future__ import annotations

import time

from app.core.metrics import metrics_store


class TimingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        method = str(scope.get("method") or "")
        path = str(scope.get("path") or "")
        key = f"{method} {path}"

        async def send_wrapper(message):
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"x-response-time-ms", b"0.00"))
                message["headers"] = headers
            if message.get("type") == "http.response.body" and not message.get("more_body", False):
                elapsed_ms = (time.perf_counter() - start) * 1000.0
                metrics_store.record(key, elapsed_ms)
            await send(message)

        await self.app(scope, receive, send_wrapper)
