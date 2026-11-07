"""Write the API's OpenAPI document for the generated frontend client.

The app is built in-process exactly as the standalone server mounts it, with
synthetic settings and a temporary data folder. Nothing connects to a service.
"""

from __future__ import annotations

import json
import sys
import tempfile
import warnings
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from fastapi import FastAPI


@contextmanager
def schema_app() -> Iterator[FastAPI]:
    """The app as the frontend sees it: the standalone server's routers."""
    from .api import create_app
    from .runtime import Runtime
    from .settings import Settings

    with tempfile.TemporaryDirectory() as data_dir:
        settings = Settings(
            _env_file=None,  # pyright: ignore[reportCallIssue]  pydantic-settings accepts it at runtime.
            data_dir=Path(data_dir), encryption_key=Fernet.generate_key().decode(),
            public_url="http://127.0.0.1:8787", gateway_url="http://127.0.0.1:8000/v1",
            gateway_key="synthetic-gateway", default_model="openai/gpt-6-astra",
        )
        yield create_app(Runtime(settings), run_dispatcher=False)


def openapi_document() -> dict[str, Any]:
    with schema_app() as app, warnings.catch_warnings():
        # Duplicate operation IDs are reported below as a hard failure.
        warnings.simplefilter("ignore", UserWarning)
        document = app.openapi()
    duplicates = [
        name for name, count in Counter(
            operation["operationId"] for item in document["paths"].values() for operation in item.values()
        ).items() if count > 1
    ]
    if duplicates:
        raise SystemExit(f"Duplicate OpenAPI operation IDs: {', '.join(sorted(duplicates))}")
    return document


def main(argv: list[str]) -> None:
    if len(argv) != 1:
        raise SystemExit("usage: python -m modelmesh.openapi_dump <output.json>")
    Path(argv[0]).write_text(json.dumps(openapi_document(), indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
