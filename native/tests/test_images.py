"""Image input and upload ownership regressions."""
import asyncio
import io

import httpx
import pytest
from PIL import Image
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.testing import WorkflowEnvironment
from test_application_boundaries import sign_in

from modelmesh.api import COOKIE, create_app
from modelmesh.images import MAX_UPLOAD_BYTES
from modelmesh.models import User, new_id
from modelmesh.storage import encrypted_converter
from modelmesh.worker import make_worker
from modelmesh.workflow import TurnWorkflow


def image_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (24, 24), "#44aaee").save(stream, format="PNG")
    return stream.getvalue()


@pytest.mark.parametrize("runtime", ["image"], indirect=True)
async def test_uploaded_image_reaches_native_temporal_input_and_saved_history(runtime, actor):
    raw = image_bytes()
    async with sign_in(runtime) as client:
        uploaded = await client.post("/api/uploads?name=clipboard.png", content=raw)
        assert uploaded.status_code == 201
        image = uploaded.json()
        session = (await client.post("/api/sessions", json={})).json()
        created = await client.post(f"/api/sessions/{session['id']}/messages", json={
            "attachments": [image["id"]], "request_key": "image-only-message",
        })
        assert created.status_code == 202
        turn = created.json()
        await runtime.database.claim_next()
        environment = await WorkflowEnvironment.start_local(
            plugins=[PydanticAIPlugin()], data_converter=encrypted_converter(runtime.security, runtime.objects),
        )
        async with environment:
            runtime.client = environment.client
            async with make_worker(runtime):
                result = await asyncio.wait_for(runtime.client.execute_workflow(
                    TurnWorkflow.run, turn["id"], id=f"modelmesh-turn-{turn['id']}",
                    task_queue=runtime.settings.temporal_queue,
                ), 45)
        assert result["status"] == "completed"
        messages = (await client.get(f"/api/sessions/{session['id']}/messages")).json()
        assert messages[0]["attachments"] == [image]
        assert "PNG image received natively" in messages[1]["content"]
        assert messages[1]["attachments"] == []
        assert (await client.get(f"/api/uploads/{image['id']}")).content == raw


async def test_images_reject_invalid_content_and_cross_conversation_reuse(runtime):
    async with sign_in(runtime) as client:
        bad = await client.post("/api/uploads?name=image.png", content=b"<script>not an image</script>")
        assert bad.status_code == 422
        huge = await client.post("/api/uploads", content=b"x" * (MAX_UPLOAD_BYTES + 1))
        assert huge.status_code == 413
        image = (await client.post("/api/uploads?name=picture.png", content=image_bytes())).json()
        first = (await client.post("/api/sessions", json={})).json()
        second = (await client.post("/api/sessions", json={"visibility": "shared"})).json()
        body = {"prompt": "Inspect it.", "attachments": [image["id"]], "request_key": "same-upload"}
        assert (await client.post(f"/api/sessions/{first['id']}/messages", json=body)).status_code == 202
        assert (await client.post(f"/api/sessions/{second['id']}/messages", json=body)).status_code == 404
        assert (await client.get(f"/api/sessions/{second['id']}/messages")).json() == []


@pytest.mark.parametrize("name", ["large.png", "unnamed"])
async def test_valid_image_over_eight_mib_uses_general_file_limit(runtime, name):
    stream = io.BytesIO()
    Image.new("RGB", (1800, 1800), "#44aaee").save(stream, format="PNG", compress_level=0)
    raw = stream.getvalue()
    assert 8 * 1024 * 1024 < len(raw) < MAX_UPLOAD_BYTES
    async with sign_in(runtime) as client:
        response = await client.post(f"/api/uploads?name={name}", content=raw)
        assert response.status_code == 201
        image = response.json()
        assert image["size"] == len(raw) and image["media_type"] == "image/png"
        assert (await client.get(f"/api/uploads/{image['id']}")).content == raw


async def test_corrupt_png_is_rejected_without_an_internal_server_error(runtime):
    raw = bytearray(image_bytes())
    raw[raw.index(b"IDAT") + 4] ^= 255
    async with sign_in(runtime) as client:
        response = await client.post("/api/uploads?name=corrupt.png", content=bytes(raw))
        assert response.status_code == 422


async def test_private_upload_is_not_readable_or_submittable_by_another_user(runtime):
    async with runtime.database.sessions.begin() as db:
        other = User(id=new_id(), username="other", display_name="Other", password="unused")
        db.add(other)
    async with sign_in(runtime) as owner, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, run_dispatcher=False)),
        base_url="http://testserver",
    ) as client:
        image = (await owner.post("/api/uploads", content=image_bytes())).json()
        token, csrf = runtime.security.issue_session(other.id, other.auth_version)
        client.cookies.set(COOKIE, token)
        client.headers["X-ModelMesh-CSRF"] = csrf
        assert (await client.get(f"/api/uploads/{image['id']}")).status_code == 404
        session = (await client.post("/api/sessions", json={})).json()
        response = await client.post(f"/api/sessions/{session['id']}/messages", json={
            "attachments": [image["id"]], "request_key": "stolen-upload",
        })
        assert response.status_code == 404

