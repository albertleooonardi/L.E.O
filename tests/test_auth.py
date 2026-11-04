import uuid

from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from backend.api import create_app
from backend.auth import Auth
from backend.schemas import Connection
from backend.store import Store, connections, user_sessions
from .conftest import add_provider, make_run


def second_user(store):
    """Another account with its own signed-in client (the fixture's user is the admin)."""
    user = store.create_user('intruder', Auth.hash_password('intruder-pass'))
    token = Auth(store, store.config).create_session(user['id'])
    client = TestClient(create_app(store.config, store))
    client.cookies.set('leo_session', token)
    return user, client


def test_sessions_are_looked_up_by_digest_not_scanned(client, store):
    with store.engine.connect() as db:
        stored = db.execute(select(user_sessions.c.token_hash)).scalars().all()
    assert stored and all(len(h) == 64 and store.user_session_token not in h for h in stored)
    assert client.get('/api/auth/me').json()['username'] == 'testuser'
    with TestClient(create_app(store.config, store)) as stranger:
        stranger.cookies.set('leo_session', 'not-a-real-session')
        assert stranger.get('/api/conversations').status_code == 401


def test_login_sets_cookie_without_exposing_the_token(store):
    with TestClient(create_app(store.config, store)) as client:
        response = client.post('/api/auth/login', json={'username': 'testuser', 'password': 'testpass123'})
        assert response.status_code == 200
        assert 'token' not in response.json() and response.cookies.get('leo_session')
        assert client.get('/api/auth/me').status_code == 200
        assert client.post('/api/auth/logout').status_code == 200
        assert client.get('/api/auth/me').status_code == 401


def test_repeated_failed_logins_are_throttled(store):
    with TestClient(create_app(store.config, store)) as client:
        for _ in range(10):
            assert client.post('/api/auth/login', json={'username': 'testuser', 'password': 'wrong-pass'}).status_code == 401
        assert client.post('/api/auth/login', json={'username': 'testuser', 'password': 'testpass123'}).status_code == 429


def test_registration_is_closed_after_the_first_account(client, store):
    with TestClient(create_app(store.config, store)) as anonymous:
        assert anonymous.post('/api/auth/register', json={'username': 'walk-in', 'password': 'password123'}).status_code == 403
    # The admin (first account) can add people; a taken name is a conflict, not a server error.
    assert client.post('/api/auth/register', json={'username': 'colleague', 'password': 'password123'}).status_code == 201
    assert client.post('/api/auth/register', json={'username': 'colleague', 'password': 'password123'}).status_code == 409
    store.config.allow_registration = True
    with TestClient(create_app(store.config, store)) as anonymous:
        assert anonymous.post('/api/auth/register', json={'username': 'invited', 'password': 'password123'}).status_code == 201


def test_server_token_deployments_need_the_token_to_create_the_first_account(tmp_path):
    from backend.config import Config
    fresh = Store(Config(data_dir=tmp_path / 'fresh'))
    fresh.config.api_token = 'server-secret'
    try:
        with TestClient(create_app(fresh.config, fresh)) as client:
            body = {'username': 'owner', 'password': 'password123'}
            assert client.post('/api/auth/register', json=body).status_code == 403
            assert client.post('/api/auth/register', json=body, headers={'Authorization': 'Bearer server-secret'}).status_code == 201
    finally:
        fresh.engine.dispose()


def test_first_account_adopts_data_saved_before_accounts(tmp_path):
    from backend.config import Config
    fresh = Store(Config(data_dir=tmp_path / 'legacy'))
    try:
        with fresh.engine.begin() as db:
            db.execute(insert(connections).values(id='legacy', config=Connection(name='Old', base_url='http://local.test/v1', model='m', free=True).model_dump(),
                                                  secret=None, cooldown_until=0))
        with TestClient(create_app(fresh.config, fresh)) as client:
            owner = client.post('/api/auth/register', json={'username': 'owner', 'password': 'password123'}).json()
        assert fresh.get_connection('legacy', owner['id']) is not None
    finally:
        fresh.engine.dispose()


def test_shared_settings_and_skills_are_admin_only(client, store):
    _, intruder = second_user(store)
    with intruder:
        prefs = intruder.get('/api/settings').json()
        assert intruder.put('/api/settings', json={**prefs, 'allow_paid': True}).status_code == 403
        assert intruder.delete('/api/skills/' + store.list_skills()[0]['id']).status_code == 403
        assert intruder.post('/api/search/test').status_code == 403
    assert client.put('/api/settings', json={**prefs, 'allow_paid': True}).status_code == 200


def test_users_cannot_post_into_or_delete_each_others_conversations(client, store):
    add_provider(store)
    run = make_run(store, prompt='private history')
    conversation_id = run['conversation_id']
    with store.engine.begin() as db:
        from backend.store import conversations, update
        db.execute(update(conversations).where(conversations.c.id == conversation_id).values(active_run=None))
    intruder_user, intruder = second_user(store)
    store.save_connection(Connection(name='Theirs', base_url='http://local.test/v1', model='m', free=True, local=True).model_dump(), user_id=intruder_user['id'])
    with intruder:
        posted = intruder.post('/api/runs', json={'prompt': 'leak it', 'request_id': str(uuid.uuid4()), 'conversation_id': conversation_id})
        assert posted.status_code == 409
        assert intruder.delete('/api/conversations/' + conversation_id).status_code == 404
    kept = store.conversation(conversation_id, store.user_id)
    assert [m['content'] for m in kept['messages']] == ['private history'] and kept['runs']


def test_cors_preflight_is_not_blocked_by_sign_in(store):
    with TestClient(create_app(store.config, store)) as client:
        response = client.options('/api/runs', headers={'Origin': 'http://localhost:4321', 'Access-Control-Request-Method': 'POST',
                                                        'Access-Control-Request-Headers': 'content-type'})
        assert response.status_code == 200
        assert response.headers['access-control-allow-origin'] == 'http://localhost:4321'


def test_server_token_can_still_start_runs(store):
    add_provider(store)
    store.config.api_token = 'server-secret'
    with TestClient(create_app(store.config, store)) as client:
        response = client.post('/api/runs', json={'prompt': 'Hi', 'request_id': str(uuid.uuid4())}, headers={'Authorization': 'Bearer server-secret'})
        assert response.status_code == 202
