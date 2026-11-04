"""Agent-mode benchmark: three end-to-end tasks, each checked by hidden tests run inside the agent's own sandbox.

1. build  - build a small app from an empty workspace, with tests.
2. fix    - make a project's failing tests pass without breaking the passing ones.
3. long   - add authentication to an app and document it: a long run that should delegate to a research and a
            testing sub-agent and survive a worker restart.

`files` are uploaded before the run. `hidden` files are written only after the run, then `check` runs in the
sandbox; a non-zero exit code fails the task. The model never sees the hidden files.
"""

NOTES_APP = {
    "app/__init__.py": "",
    "app/main.py": '''from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Notes")
NOTES: dict[int, dict] = {}


class NoteIn(BaseModel):
    title: str
    body: str = ""


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/notes", status_code=201)
def create_note(note: NoteIn):
    note_id = len(NOTES) + 1
    NOTES[note_id] = {"id": note_id, **note.model_dump()}
    return NOTES[note_id]


@app.get("/notes")
def list_notes():
    return list(NOTES.values())


@app.get("/notes/{note_id}")
def get_note(note_id: int):
    if note_id not in NOTES:
        raise HTTPException(404, "Note not found")
    return NOTES[note_id]
''',
    "requirements.txt": "fastapi\nhttpx\npytest\n",
    "tests/test_notes.py": '''from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    assert client.get("/health").json() == {"status": "ok"}
''',
    "README.md": "# Notes\n\nA small notes API. Run it with `uvicorn app.main:app`.\n",
}

TEXTSTATS = {
    "textstats/__init__.py": "from .core import word_count, top_words, reading_time\n",
    "textstats/core.py": '''import re
from collections import Counter

WORD = re.compile(r"[a-z']+")


def words(text):
    return WORD.findall(text)


def word_count(text):
    """Number of words; case does not matter."""
    return len(words(text))


def top_words(text, n=3):
    """The n most common words, most common first; ties in order of first appearance."""
    counts = Counter(words(text.lower()))
    return [w for w, _ in counts.most_common(n + 1)]


def reading_time(text, wpm=200):
    """Whole minutes to read the text, rounded up; empty text takes 0 minutes."""
    return word_count(text) // wpm
''',
    "tests/test_core.py": '''from textstats import reading_time, top_words, word_count


def test_word_count_ignores_case():
    assert word_count("Hello hello WORLD") == 3


def test_top_words():
    assert top_words("b a b c a b", 2) == ["b", "a"]


def test_reading_time_rounds_up():
    assert reading_time("word " * 201) == 2
    assert reading_time("") == 0


def test_apostrophes_are_part_of_words():
    assert word_count("don't stop") == 2
''',
}

AGENT_TASKS = [
    {
        "id": "build",
        "prompt": "Build a todo app in this empty workspace: a FastAPI backend that stores todos in SQLite (todo.db) and serves a "
                  "small HTML page at / to add, complete, and delete todos. API: POST /todos {title} -> 201 with {id, title, done}; "
                  "GET /todos; PATCH /todos/{id} {done}; DELETE /todos/{id} -> 204; unknown ids return 404. Put the app in app.py "
                  "(the FastAPI instance is named app), list dependencies in requirements.txt, and write pytest tests in tests/ "
                  "that cover every endpoint. Install what you need and make the tests pass.",
        "files": {},
        "hidden": {"hidden_test_todo.py": '''import importlib, os, sys
sys.path.insert(0, "/workspace")
os.chdir("/tmp")
from fastapi.testclient import TestClient
import app as module
client = TestClient(module.app)

def test_contract():
    created = client.post("/todos", json={"title": "Write tests"})
    assert created.status_code == 201
    todo = created.json()
    assert todo["title"] == "Write tests" and todo["done"] is False
    assert any(t["id"] == todo["id"] for t in client.get("/todos").json())
    assert client.patch(f"/todos/{todo['id']}", json={"done": True}).json()["done"] is True
    assert client.delete(f"/todos/{todo['id']}").status_code == 204
    assert client.delete(f"/todos/{todo['id']}").status_code == 404
    assert client.patch("/todos/999999", json={"done": True}).status_code == 404
    assert "<" in client.get("/").text
'''},
        "check": "cd /workspace && python -m pytest -q tests && python -m pytest -q -p no:cacheprovider hidden_test_todo.py",
    },
    {
        "id": "fix",
        "prompt": "The tests in this project fail. Find and fix the bugs in textstats/core.py so that every test passes. Do not change "
                  "the tests, and keep the documented behavior in the docstrings.",
        "files": TEXTSTATS,
        "hidden": {"hidden_test_textstats.py": '''import sys
sys.path.insert(0, "/workspace")
from textstats import reading_time, top_words, word_count

def test_more_cases():
    assert word_count("It's IT's it's") == 3
    assert top_words("x y y x z", 2) == ["x", "y"]
    assert top_words("one", 3) == ["one"]
    assert reading_time("w " * 200) == 1 and reading_time("w " * 400, wpm=100) == 4
'''},
        "check": "cd /workspace && python -m pytest -q tests hidden_test_textstats.py",
        "unchanged": ["tests/test_core.py"],
    },
    {
        "id": "long",
        "prompt": "Add authentication to this notes API and document it. Use a research sub-agent to compare token-based options for "
                  "FastAPI and recommend one, and a testing sub-agent to write the tests. Requirements: POST /auth/register "
                  "{username, password} -> 201; POST /auth/login {username, password} -> 200 {access_token, token_type: \"bearer\"}; "
                  "every /notes endpoint requires 'Authorization: Bearer <token>' and returns 401 without a valid one; notes belong "
                  "to the user who created them (other users get 404); passwords are hashed, never stored in plain text; /health "
                  "stays public. Update README.md with how authentication works and how to call the API, and keep all tests passing.",
        "files": NOTES_APP,
        "hidden": {"hidden_test_auth.py": '''import sys
sys.path.insert(0, "/workspace")
from fastapi.testclient import TestClient
from app.main import app
client = TestClient(app)

def token(name):
    assert client.post("/auth/register", json={"username": name, "password": "s3cret-pass"}).status_code == 201
    response = client.post("/auth/login", json={"username": name, "password": "s3cret-pass"})
    assert response.status_code == 200 and response.json()["token_type"] == "bearer"
    return {"Authorization": f"Bearer {response.json()['access_token']}"}

def test_auth_contract():
    assert client.get("/health").status_code == 200
    assert client.get("/notes").status_code == 401
    assert client.get("/notes", headers={"Authorization": "Bearer nonsense"}).status_code == 401
    alice, bob = token("alice"), token("bob")
    note = client.post("/notes", json={"title": "secret"}, headers=alice).json()
    assert client.get(f"/notes/{note['id']}", headers=alice).status_code == 200
    assert client.get(f"/notes/{note['id']}", headers=bob).status_code == 404
    assert client.post("/auth/login", json={"username": "alice", "password": "wrong"}).status_code == 401
'''},
        "check": "cd /workspace && python -m pytest -q tests hidden_test_auth.py && grep -qi 'auth' README.md && "
                 "! grep -rq 's3cret-pass' --include='*.db' --include='*.json' . 2>/dev/null",
        "expect": {"subagent_skills": ["research", "coding"], "min_steps": 50},
    },
]
