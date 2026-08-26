from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
MODEL = "qwen2.5:7b"
MAX_PAGE_CHARS = 12000

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["*"],
)


class AskRequest(BaseModel):
    page_text: str
    question: str


@app.post("/ask")
async def ask(req: AskRequest):
    page = req.page_text[:MAX_PAGE_CHARS]
    prompt = (
        "You are answering a question about the contents of a web page.\n"
        "Use only the page text below. If the answer is not in the page, say so.\n\n"
        f"--- PAGE TEXT ---\n{page}\n--- END PAGE TEXT ---\n\n"
        f"Question: {req.question}\n\nAnswer:"
    )
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            OLLAMA_URL,
            json={"model": MODEL, "prompt": prompt, "stream": False},
        )
        r.raise_for_status()
        data = r.json()
    return {"answer": data.get("response", "").strip()}
