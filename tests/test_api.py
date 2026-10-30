import json
import uuid

from fastapi.testclient import TestClient

from backend.api import create_app
from backend.schemas import Connection, RunInput
from .conftest import add_provider, make_run


def test_keys_are_encrypted_and_never_returned(client, store):
    body = Connection(name='Secret test', base_url='https://example.test/v1', model='test', api_key='secret-value-never-echo').model_dump()
    response = client.post('/api/connections', json=body)
    assert response.status_code == 201
    saved = response.json()
    assert saved['has_key']
    assert 'secret-value' not in response.text
    assert 'secret-value' not in client.get('/api/connections').text
    assert store.decrypt(store.get_connection(saved['id'], True)) == 'secret-value-never-echo'
    with store.engine.connect() as db:
        from backend.store import connections
        from sqlalchemy import select
        assert 'secret-value' not in str(db.execute(select(connections)).all())
    body['api_key'] = None
    client.put('/api/connections/' + saved['id'], json=body)
    assert store.get_connection(saved['id'])['has_key']
    body['api_key'] = ''
    client.put('/api/connections/' + saved['id'], json=body)
    assert not store.get_connection(saved['id'])['has_key']


def test_invalid_settings_and_urls_do_not_echo_secrets(client):
    response = client.post('/api/connections', json={'name': 'bad', 'base_url': 'file:///tmp/test', 'model': 'x', 'api_key': 'hidden-value'})
    assert response.status_code == 422
    assert 'hidden-value' not in response.text
    response = client.put('/api/settings', json={**client.get('/api/settings').json(), 'context_tokens': 1024, 'max_output_tokens': 2048})
    assert response.status_code == 422


def test_idempotent_submission_and_one_active_run(client, store):
    add_provider(store)
    body = {'prompt': 'Hello', 'request_id': str(uuid.uuid4())}
    first = client.post('/api/runs', json=body)
    second = client.post('/api/runs', json=body)
    assert first.status_code == second.status_code == 202
    assert first.json()['id'] == second.json()['id']
    conflict = client.post('/api/runs', json={**body, 'request_id': str(uuid.uuid4()), 'conversation_id': first.json()['conversation_id']})
    assert conflict.status_code == 409
    assert len(client.get('/api/conversations').json()) == 1


def test_cancel_queued_job_without_worker(client, store):
    add_provider(store)
    run = make_run(store)
    assert client.post(f"/api/runs/{run['id']}/cancel").status_code == 200
    assert store.get_run(run['id'])['status'] == 'cancelled'
    assert store.conversation(run['conversation_id'])['active_run'] is None
    assert client.delete(f"/api/conversations/{run['conversation_id']}").status_code == 200


def test_paid_connections_require_opt_in(client, store):
    store.save_connection(Connection(name='Paid', base_url='https://example.test/v1', model='gpt', free=False).model_dump())
    assert client.post('/api/runs', json={'prompt': 'Hi', 'request_id': str(uuid.uuid4())}).status_code == 400


def test_access_token_and_origin_guards(store):
    store.config.api_token = 'server-secret'
    with TestClient(create_app(store.config, store)) as client:
        assert client.get('/api/health').status_code == 200
        assert client.get('/api/settings').status_code == 401
        assert client.post('/api/session', json={'token': 'wrong'}).status_code == 401
        assert client.post('/api/session', json={'token': 'server-secret'}, headers={'Origin': 'https://evil.test'}).status_code == 403
        assert client.post('/api/session', json={'token': 'server-secret'}).status_code == 200
        assert client.get('/api/settings').status_code == 200
        assert client.get('/api/settings', headers={'Host': 'attacker.test'}).status_code == 400
        assert client.delete('/api/session').status_code == 200
        assert client.get('/api/settings').status_code == 401


def test_deleting_connection_clears_role(store):
    provider = add_provider(store)
    store.set_preferences({**store.preferences(), 'generator_id': provider['id']})
    store.delete_connection(provider['id'])
    assert store.preferences()['generator_id'] is None


def test_storage_and_keys_survive_reinitialization(store):
    from backend.store import Store
    provider = store.save_connection(Connection(name='Persistent', base_url='http://test/v1', model='test', api_key='stored-secret', free=True).model_dump())
    run = make_run(store)
    reopened = Store(store.config)
    try:
        assert reopened.decrypt(reopened.get_connection(provider['id'], True)) == 'stored-secret'
        assert reopened.get_run(run['id'])['status'] == 'queued'
    finally:
        reopened.engine.dispose()


def test_remote_hostname_requires_access_token(store):
    import pytest
    store.config.hosts = ['127.0.0.1', 'leo.example.com']
    with pytest.raises(RuntimeError, match='LEO_API_TOKEN'):
        create_app(store.config, store)
    store.config.api_token = 'owner-token'
    with TestClient(create_app(store.config, store)) as client:
        assert client.get('/api/settings', headers={'Host': 'leo.example.com'}).status_code == 401


def test_one_key_serves_many_models_on_the_same_host_only(client, store):
    first = client.post('/api/connections', json=Connection(name='OR A', base_url='https://openrouter.ai/api/v1', model='a', api_key='shared-secret-value').model_dump()).json()
    second = client.post('/api/connections', json=Connection(name='OR B', base_url='https://openrouter.ai/api/v1', model='b', key_from=first['id']).model_dump())
    assert second.status_code == 201 and second.json()['has_key'] and 'shared-secret' not in second.text
    assert store.decrypt(store.get_connection(second.json()['id'], True)) == 'shared-secret-value'
    stolen = client.post('/api/connections', json=Connection(name='Elsewhere', base_url='https://evil.test/v1', model='x', key_from=first['id']).model_dump())
    assert stolen.status_code == 400 and 'same provider host' in stolen.json()['detail']
    assert 'key_from' not in store.get_connection(second.json()['id'])


def test_search_test_explains_missing_setup_and_settings_validate_url(client):
    response = client.post('/api/search/test')
    assert response.status_code == 400 and 'not set up' in response.json()['detail']
    bad = client.put('/api/settings', json={**client.get('/api/settings').json(), 'search_url': 'ftp://x'})
    assert bad.status_code == 422
    good = client.put('/api/settings', json={**client.get('/api/settings').json(), 'search_url': 'http://localhost:8888/'})
    assert good.status_code == 200 and good.json()['search_url'] == 'http://localhost:8888'
