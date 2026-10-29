import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.api import create_app
from backend.config import Config
from backend.schemas import Connection, RunInput
from backend.store import Store


@pytest.fixture
def store(tmp_path):
    value = Store(Config(data_dir=tmp_path))
    yield value
    value.engine.dispose()


@pytest.fixture
def client(store):
    with TestClient(create_app(store.config, store)) as value:
        yield value


def add_provider(store, **kwargs):
    return store.save_connection(Connection(name='Local test', protocol='openai', base_url='http://local.test/v1', model='test-model', free=True, local=True, **kwargs).model_dump())


def make_run(store, mode='quick', prompt='Write a Python add function', **kwargs):
    import uuid
    return store.create_run(RunInput(prompt=prompt, mode=mode, request_id=str(uuid.uuid4()), **kwargs))


def sse_response(text, protocol='openai', usage=True):
    if protocol == 'anthropic':
        data = [
            {'type': 'message_start', 'message': {'usage': {'input_tokens': 25, 'output_tokens': 0}}},
            {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': text}},
            {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 12}},
            {'type': 'message_stop'},
        ]
    elif protocol == 'responses':
        data = [
            {'type': 'response.output_text.delta', 'delta': text},
            {'type': 'response.completed', 'response': {'usage': {'input_tokens': 25, 'output_tokens': 12}}},
        ]
    else:
        data = [{'choices': [{'delta': {'content': text}, 'finish_reason': None}]},
                {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}]
        if usage:
            data.append({'choices': [], 'usage': {'prompt_tokens': 25, 'completion_tokens': 12}})
    content = ''.join('data: ' + json.dumps(d) + '\n\n' for d in data)
    if protocol == 'openai':
        content += 'data: [DONE]\n\n'
    return httpx.Response(200, text=content, headers={'content-type': 'text/event-stream'})
