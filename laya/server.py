"""ModelMesh's task-type classifier over HTTP: Laya with the heads that train.py fitted.

POST /classify {"text": "..."} -> {"task_type", "confidence", "route"}; route "review" means Laya is unsure.
GET /health -> 200 when a classifier is loaded, 503 before the first training.
/classify needs `Authorization: Bearer $LAYA_API_KEY`. The service has no published port: only ModelMesh reaches it.
"""
import hmac
import json
import os
import threading
import time
from pathlib import Path

import laya
import torch
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

KEY = os.environ.get("LAYA_API_KEY", "")
if not KEY:
    raise SystemExit("Set LAYA_API_KEY.")
if os.environ.get("LAYA_THREADS"):
    torch.set_num_threads(int(os.environ["LAYA_THREADS"]))
TARGET = Path(os.environ.get("LAYA_CLASSIFIER", "/models/modelmesh-task-type"))
QUESTIONS, agent = None, None
if (TARGET / "questions.json").exists():
    QUESTIONS = json.loads((TARGET / "questions.json").read_text(encoding="utf-8"))
    agent = laya.Agent(vegaml=True)
    agent.load_calibration(str(TARGET))
    # The first prompts of new lengths are slow (27-39 s measured); pay that here, not on a user's message.
    for text in ("hi", "Write a Python function that checks whether a string is a palindrome, with tests.",
                 "Summarize the following notes for my manager in three bullet points. " + "The team met on Monday. " * 60):
        agent.predict(text, QUESTIONS)
lock = threading.Lock()  # ponytail: one classification at a time; enough for chat traffic, add workers if requests queue.


def keep_warm() -> None:
    # Insurance against long idle periods (after hours idle the first two classifications took over 6 s). It does not
    # stop a few idle minutes from slowing the next answer to 4-7 s (twice the CPU time, no swap reads measured), so
    # the app waits up to 15 s for Laya. ponytail: fixed 5-minute ping, about 6 CPU-seconds each (2-3% of one core).
    while True:
        time.sleep(300)
        with lock:
            agent.predict("hi", QUESTIONS)


if agent is not None:
    threading.Thread(target=keep_warm, daemon=True).start()
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


class Task(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)


@app.get("/health")
def health() -> dict:
    if agent is None:
        raise HTTPException(503, "No classifier yet: run train.py.")
    return {"ready": True}


@app.post("/classify")
def classify(task: Task, authorization: str = Header("")) -> dict:
    if not hmac.compare_digest(authorization.encode(), f"Bearer {KEY}".encode()):
        raise HTTPException(401, "Wrong or missing key.")
    if agent is None:
        raise HTTPException(503, "No classifier yet: run train.py.")
    with lock:
        answer = agent.predict(task.text, QUESTIONS)["answers"]["task_type"]
    return {"task_type": answer["choice"], "confidence": answer.get("answer_confidence"), "route": answer.get("route")}
