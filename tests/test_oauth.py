import base64
import hashlib
import re
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from backend.api import create_app
from backend.auth import Auth
from backend.config import Config
from backend.oauth import OAuth, email_allowed
from backend.store import Store


class FakeProviders:
    """Google, GitHub and an OIDC issuer. A test queues the profile the next code exchange will return."""

    def __init__(self):
        self.profile, self.challenge, self.exchanged = None, None, []

    def __call__(self, request):
        url = str(request.url)
        if url == 'https://sso.example.com/.well-known/openid-configuration':
            return httpx.Response(200, json={'authorization_endpoint': 'https://sso.example.com/auth', 'token_endpoint': 'https://sso.example.com/token',
                                             'userinfo_endpoint': 'https://sso.example.com/userinfo'})
        if request.method == 'POST' and url in ('https://oauth2.googleapis.com/token', 'https://github.com/login/oauth/access_token', 'https://sso.example.com/token'):
            form = parse_qs(request.content.decode())
            self.exchanged.append(form)
            verifier = form['code_verifier'][0]
            # PKCE: the verifier must hash to the challenge sent with the authorization request.
            if base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode() != self.challenge:
                return httpx.Response(400, json={'error': 'invalid_grant'})
            return httpx.Response(200, json={'access_token': 'provider-token', 'token_type': 'bearer'})
        assert request.headers['authorization'] == 'Bearer provider-token'
        if url in ('https://openidconnect.googleapis.com/v1/userinfo', 'https://sso.example.com/userinfo'):
            return httpx.Response(200, json=self.profile)
        if url == 'https://api.github.com/user':
            return httpx.Response(200, json=self.profile['user'])
        if url == 'https://api.github.com/user/emails':
            return httpx.Response(200, json=self.profile['emails'])
        return httpx.Response(404)


@pytest.fixture
def providers(store):
    fake = FakeProviders()
    config = store.config
    config.google_client_id, config.google_client_secret = 'g-id', 'g-secret'
    config.github_client_id, config.github_client_secret = 'gh-id', 'gh-secret'
    config.oidc_issuer, config.oidc_client_id, config.oidc_client_secret, config.oidc_name = 'https://sso.example.com', 'o-id', 'o-secret', 'Acme SSO'
    config.allowed_emails = ['@example.com', 'guest@other.org']
    fake.oauth = OAuth(config, httpx.MockTransport(fake))
    return fake


def app_client(store, providers, session=None):
    client = TestClient(create_app(store.config, store, oauth=providers.oauth))
    if session:
        client.cookies.set('leo_session', session)
    return client


def sign_in(client, providers, provider, profile, next_path=None, link=False, **callback):
    """Run the browser's side of an OAuth sign-in; returns where the callback page sends the browser."""
    params = {'next': next_path} if next_path else {}
    if link:
        params['link'] = '1'
    start = client.get(f'/api/auth/oauth/{provider}/start', params=params, follow_redirects=False)
    assert start.status_code == 307, start.text
    query = parse_qs(urlsplit(start.headers['location']).query)
    assert query['code_challenge_method'] == ['S256'] and query['redirect_uri'] == [f'http://testserver/api/auth/oauth/{provider}/callback']
    providers.profile, providers.challenge = profile, query['code_challenge'][0]
    page = client.get(f'/api/auth/oauth/{provider}/callback', params={'code': 'one-time-code', 'state': query['state'][0], **callback})
    assert page.status_code == 200 and 'no-store' in page.headers['cache-control']
    # A page that navigates itself, so the SameSite=Strict session cookie is sent on the next request.
    return re.search(r'url=([^"]+)"', page.text)[1].replace('&amp;', '&')


def google(email='ann@example.com', verified=True, sub='g-1'):
    return {'sub': sub, 'email': email, 'email_verified': verified, 'name': 'Ann Example', 'picture': 'https://img.example.com/ann.png'}


def github(email='ann@example.com', login='ann-gh', uid=42):
    return {'user': {'id': uid, 'login': login, 'name': None, 'avatar_url': 'https://avatars.example.com/42', 'email': None},
            'emails': [{'email': 'old@unverified.net', 'primary': False, 'verified': False}, {'email': email, 'primary': True, 'verified': True}]}


def test_allowlist_rules():
    rules = ['@example.com', 'guest@other.org']
    assert email_allowed('Ann@Example.com', rules) and email_allowed('guest@other.org', rules)
    assert not email_allowed('ann@example.com.evil.io', rules) and not email_allowed('host@other.org', rules) and not email_allowed(None, rules)


def test_google_sign_in_creates_an_invited_account(store, providers):
    with app_client(store, providers) as client:
        assert sign_in(client, providers, 'google', google(), next_path='/usage') == '/usage'
        me = client.get('/api/auth/me').json()
    assert me['username'] == 'ann' and me['email'] == 'ann@example.com' and me['display_name'] == 'Ann Example'
    assert me['identities'] == ['google'] and me['has_password'] is False
    assert providers.exchanged[0]['client_secret'] == ['g-secret']


def test_people_outside_the_allowlist_are_turned_away(store, providers):
    with app_client(store, providers) as client:
        target = sign_in(client, providers, 'google', google('mallory@elsewhere.net'))
        assert target == '/login?error=not_invited&email=mallory%40elsewhere.net'
        assert client.get('/api/auth/me').status_code == 401
        # The allowlist is checked on every sign-in, so removing someone locks them out.
        assert sign_in(client, providers, 'google', google()) == '/'
        client.cookies.clear()
        store.config.allowed_emails = ['guest@other.org']
        assert sign_in(client, providers, 'google', google()).startswith('/login?error=not_invited')


def test_github_signs_in_to_the_same_account_by_verified_email(store, providers):
    with app_client(store, providers) as client:
        sign_in(client, providers, 'google', google())
        first = client.get('/api/auth/me').json()
        client.cookies.clear()
        assert sign_in(client, providers, 'github', github()) == '/'
        again = client.get('/api/auth/me').json()
    assert again['id'] == first['id'] and again['identities'] == ['google', 'github']


def test_github_login_names_a_new_account(store, providers):
    with app_client(store, providers) as client:
        sign_in(client, providers, 'github', github('dev@example.com', login='Dev.Person'))
        assert client.get('/api/auth/me').json()['username'] == 'dev.person'


def test_an_unverified_oidc_email_is_not_trusted(store, providers):
    with app_client(store, providers) as client:
        assert sign_in(client, providers, 'oidc', google(verified=False, sub='o-1')).startswith('/login?error=not_invited')
        assert sign_in(client, providers, 'oidc', google(verified='true', sub='o-1')) == '/'
        assert client.get('/api/auth/me').json()['identities'] == ['oidc']


def test_bad_state_cancellation_and_unsafe_next(store, providers):
    with app_client(store, providers) as client:
        client.get('/api/auth/oauth/google/start', follow_redirects=False)
        page = client.get('/api/auth/oauth/google/callback', params={'code': 'x', 'state': 'forged'})
        assert 'error=failed' in page.text and not providers.exchanged
        assert sign_in(client, providers, 'google', google(), error='access_denied') == '/login?error=cancelled'
        assert sign_in(client, providers, 'google', google(), next_path='//evil.example/steal') == '/'
        assert client.get('/api/auth/oauth/twitter/start', follow_redirects=False).status_code == 404


def test_the_first_user_can_sign_up_without_an_allowlist(tmp_path):
    fresh = Store(Config(data_dir=tmp_path / 'fresh'))
    fake = FakeProviders()
    fresh.config.google_client_id, fresh.config.google_client_secret = 'g-id', 'g-secret'
    fake.oauth = OAuth(fresh.config, httpx.MockTransport(fake))
    try:
        with app_client(fresh, fake) as client:
            assert sign_in(client, fake, 'google', google('owner@anywhere.io')) == '/'
            owner = client.get('/api/auth/me').json()
            assert fresh.admin_user_id() == owner['id']
            client.cookies.clear()
            assert sign_in(client, fake, 'google', google('second@anywhere.io', sub='g-2')).startswith('/login?error=not_invited')
            # The owner can still come back: existing accounts do not need the list.
            assert sign_in(client, fake, 'google', google('owner@anywhere.io')) == '/'
    finally:
        fresh.engine.dispose()


def test_linking_and_unlinking_sign_in_methods(store, providers):
    with app_client(store, providers, store.user_session_token) as client:
        assert sign_in(client, providers, 'github', github('test@example.com'), next_path='/?settings=account', link=True) == '/?settings=account'
        me = client.get('/api/auth/me').json()
        assert me['username'] == 'testuser' and me['identities'] == ['github'] and me['has_password'] and me['email'] == 'test@example.com'
        assert client.delete('/api/auth/identities/github').status_code == 200
        assert client.get('/api/auth/me').json()['identities'] == []
        assert client.delete('/api/auth/identities/github').status_code == 404


def test_linking_a_provider_account_that_belongs_to_someone_else(store, providers):
    with app_client(store, providers) as stranger:
        sign_in(stranger, providers, 'google', google())
    with app_client(store, providers, store.user_session_token) as client:
        assert sign_in(client, providers, 'google', google(), link=True) == '/?settings=account&error=linked_elsewhere'
        assert client.get('/api/auth/me').json()['identities'] == []


def test_oauth_only_accounts_keep_one_way_in_and_can_set_a_password(store, providers):
    with app_client(store, providers) as client:
        sign_in(client, providers, 'google', google())
        assert client.delete('/api/auth/identities/google').status_code == 409
        assert client.put('/api/auth/password', json={'new_password': 'brand-new-pass'}).status_code == 200
        assert client.put('/api/auth/password', json={'new_password': 'another-pass-1'}).status_code == 400
        assert client.put('/api/auth/password', json={'current_password': 'wrong-pass', 'new_password': 'another-pass-1'}).status_code == 403
        assert client.put('/api/auth/password', json={'current_password': 'brand-new-pass', 'new_password': 'another-pass-1'}).status_code == 200
        assert client.delete('/api/auth/identities/google').status_code == 200
        client.cookies.clear()
        assert client.post('/api/auth/login', json={'username': 'ann', 'password': 'another-pass-1'}).status_code == 200


def test_password_sign_in_never_works_for_oauth_only_accounts(store, providers):
    with app_client(store, providers) as client:
        sign_in(client, providers, 'google', google())
        client.cookies.clear()
        assert client.post('/api/auth/login', json={'username': 'ann', 'password': 'anything-at-all'}).status_code == 401


def test_the_login_page_learns_which_providers_exist(store, providers):
    with TestClient(create_app(store.config, store, oauth=providers.oauth)) as anonymous:
        found = anonymous.get('/api/auth/providers').json()
    assert found == {'providers': [{'id': 'google', 'name': 'Google'}, {'id': 'github', 'name': 'GitHub'}, {'id': 'oidc', 'name': 'Acme SSO'}],
                     'password_registration': False}


def test_older_databases_gain_the_account_columns(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'leo.db'}")
    with engine.begin() as db:
        db.execute(text('CREATE TABLE users (id VARCHAR(36) PRIMARY KEY, username VARCHAR(80) UNIQUE NOT NULL, '
                        'password_hash VARCHAR(255) NOT NULL, created_at FLOAT NOT NULL)'))
        db.execute(text("INSERT INTO users VALUES ('u1', 'old-timer', 'hash', 1)"))
    engine.dispose()
    upgraded = Store(Config(data_dir=tmp_path))
    try:
        assert {'email', 'display_name', 'avatar_url'} <= {c['name'] for c in inspect(upgraded.engine).get_columns('users')}
        assert upgraded.get_user('u1')['username'] == 'old-timer'
        assert Auth(upgraded, upgraded.config).authenticate_user('old-timer', 'x') is None
    finally:
        upgraded.engine.dispose()
