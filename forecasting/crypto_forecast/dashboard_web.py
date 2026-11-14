"""Static files shared by the standalone and CandleRoute dashboards."""

from pathlib import Path

WEB = Path(__file__).with_name("web")

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/connection.js": ("connection.js", "text/javascript; charset=utf-8"),
    "/chart.js": ("chart.js", "text/javascript; charset=utf-8"),
    "/chart_view.js": ("chart_view.js", "text/javascript; charset=utf-8"),
    "/testing.js": ("testing.js", "text/javascript; charset=utf-8"),
    "/vendor/lightweight-charts.js": (
        "vendor/lightweight-charts/lightweight-charts.js", "text/javascript; charset=utf-8",
    ),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


def static_response(path):
    asset = STATIC.get(path)
    if asset is None:
        return None
    filename, media_type = asset
    return (WEB / filename).read_bytes(), media_type


def body_limit(path):
    if path in ("/api/chart/preview", "/api/chart/forecast",
                "/api/testing/chart", "/api/testing/forecast"):
        return 1_200_000
    return 65_536
