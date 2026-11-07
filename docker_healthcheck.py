"""Use the same origin and port as the container's server."""
import os
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def health_request() -> Request:
    origin = os.environ.get("PUBLIC_URL", "http://127.0.0.1:8787")
    port = os.environ.get("PORT", "8787")
    return Request(f"http://127.0.0.1:{port}/health", headers={"Host": urlparse(origin).netloc})


if __name__ == "__main__":
    with urlopen(health_request(), timeout=3) as response:
        if response.status != 200:
            raise SystemExit(1)
