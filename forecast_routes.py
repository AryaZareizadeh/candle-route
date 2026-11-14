"""Serve the forecasting workspace through CandleRoute's authenticated app."""

from threading import Lock

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from forecasting.crypto_forecast.dashboard_web import body_limit, static_response


_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors {ancestors}; "
    "base-uri 'none'; form-action 'self'"
)
_DEPENDENCIES = (
    "Forecasting needs the optional model dependencies. Install the forecasting package with "
    "its Kronos dependencies, or use the training-enabled CandleRoute image."
)


class ForecastWorkspace:
    def __init__(self, directory, *, offline):
        self.directory = directory
        self.offline = offline
        self._lock = Lock()
        self._manager = None

    def manager(self):
        with self._lock:
            if self._manager is None:
                from crypto_forecast.dashboard import JobManager

                for relative in (".", ".dashboard", ".dashboard/jobs", ".dashboard/charts",
                                 "data", "data/_archives", "data/15m", "data/1h", "data/4h", "data/1d",
                                 "runs", "models", "config.example.json"):
                    path = self.directory / relative
                    if path.resolve() != path:
                        raise ValueError("Forecasting workspace must use local, unlinked directories and settings.")
                self._manager = JobManager(self.directory, offline=self.offline)
            return self._manager

    def shutdown(self):
        with self._lock:
            if self._manager is not None:
                self._manager.close()
                self._manager = None

    def read(self, route, token):
        from crypto_forecast.dashboard import get_response

        return get_response(self.manager(), route, token)

    def write(self, route, content_type, body):
        from crypto_forecast.dashboard import post_response

        return post_response(self.manager(), route, content_type, body)


async def read_body(request, limit):
    length = request.headers.get("content-length")
    if length is not None and not 0 <= int(length) <= limit:
        raise ValueError("Invalid request size.")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise ValueError("Invalid request size.")
        body.extend(chunk)
    return body


def forecast_router(workspace):
    router = APIRouter(include_in_schema=False)

    @router.get("/forecasting")
    async def forecast_redirect():
        return RedirectResponse("/forecasting/")

    @router.api_route("/forecasting/{path:path}", methods=["GET", "HEAD", "POST"])
    async def forecast(path: str, request: Request):
        route = "/" + path
        is_page = route == "/" and request.method in {"GET", "HEAD"}
        headers = {
            "Content-Security-Policy": _CSP.format(ancestors="'self'" if is_page else "'none'"),
        }
        if request.method in {"GET", "HEAD"}:
            try:
                asset = await run_in_threadpool(static_response, route)
            except OSError:
                asset = None
            if asset is not None:
                content, media_type = asset
                return Response(content, media_type=media_type, headers=headers)
        if not route.startswith("/api/"):
            return JSONResponse({"error": "Page not found."}, 404, headers=headers)
        try:
            if request.method == "POST":
                try:
                    body = await read_body(request, body_limit(route))
                except ValueError:
                    return JSONResponse({"error": "Invalid request size."}, 400, headers=headers)
                status, payload = await run_in_threadpool(
                    workspace.write, route, request.headers.get("content-type", ""), body,
                )
            else:
                status, payload = await run_in_threadpool(
                    workspace.read, route, request.scope.get("forecast_csrf_token"),
                )
                if route == "/api/bootstrap" and status == 200:
                    payload.update(embedded=True, settings_scope=request.scope.get("user_id", "local"))
        except ImportError:
            return JSONResponse({"error": _DEPENDENCIES}, 503, headers=headers)
        except ValueError as error:
            return JSONResponse({"error": str(error)}, 409, headers=headers)
        except OSError:
            return JSONResponse({"error": "Forecasting workspace could not be opened."}, 503, headers=headers)
        return JSONResponse(payload, status, headers=headers)

    return router
