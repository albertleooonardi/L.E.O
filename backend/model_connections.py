"""L.E.O.'s model selection over per-user 9Router provider connections."""
import asyncio
from urllib.parse import quote, urlsplit

from .discovery import NOT_CHAT, discover, profile
from .ninerouter import ACCOUNTS, KEY_PROVIDERS, MODEL_PROVIDERS, RouterError
from .schemas import Connection


class ModelConnections:
    def __init__(self, store, router_for):
        self.store, self.router_for = store, router_for
        self.locks = {}

    def lock(self, uid):
        return self.locks.setdefault(uid, asyncio.Lock())

    async def key(self, uid, router):
        mine = self.store.list_connections(uid, True)
        source = next((c for c in mine if c['base_url'] == router.url + '/v1' and c.get('secret')), None)
        return self.store.decrypt(source) if source else await router.create_key()

    async def catalog(self, uid, provider):
        spec = MODEL_PROVIDERS[provider]
        router = await self.router_for(uid, create=True)
        accounts = await router.accounts()
        account = next((a for a in accounts if a['provider'] == provider and a['active']), None)
        if not account:
            raise RouterError('Connect this provider before choosing models.')
        if provider in ACCOUNTS:
            key = await self.key(uid, router)
            found = await discover({'base_url': router.url + '/v1', 'protocol': 'openai', 'free': True}, key)
            models = [m for m in found['models'] if m['model'].startswith(spec['prefix'] + '/')]
        else:
            # The router fetches the actual upstream catalog using its saved credential.
            data = await router.request('GET', f"/api/providers/{quote(account['id'], safe='')}/models")
            models = []
            for item in data.get('models') or []:
                if not isinstance(item, dict):
                    continue
                model = str(item.get('id') or item.get('name') or '').removeprefix('models/')
                if not model or NOT_CHAT.search(model) or len(model) + len(spec['prefix']) + 1 > 160:
                    continue
                output = (item.get('architecture') or {}).get('output_modalities')
                if output and 'text' not in output:
                    continue
                methods = item.get('supportedGenerationMethods')
                if methods and 'generateContent' not in methods:
                    continue
                pricing = item.get('pricing') or {}
                try:
                    free = float(pricing.get('prompt')) == 0 and float(pricing.get('completion')) == 0
                except (TypeError, ValueError):
                    free = False
                caps = item.get('capabilities') or {}
                parameters = item.get('supported_parameters') or []
                m = profile(f"{spec['prefix']}/{model}", item.get('description', ''),
                            caps.get('reasoning', 'reasoning' in parameters), item.get('context_length') or item.get('inputTokenLimit') or caps.get('contextWindow'),
                            free, item.get('displayName') or item.get('name') or model,
                            'tools' in parameters if parameters else None)
                for field, value in [('input_price', pricing.get('prompt')), ('output_price', pricing.get('completion'))]:
                    try:
                        price = float(value) * 1_000_000
                        if 0 <= price <= 10000:
                            m[field] = price
                    except (TypeError, ValueError):
                        pass
                models.append(m)
        existing = {c['model'] for c in self.store.list_connections(uid) if c['base_url'] == router.url + '/v1'}
        unique = {m['model']: {**m, 'added': m['model'] in existing} for m in models}
        return {'provider': provider, 'name': spec['name'], 'models': sorted(unique.values(), key=lambda m: (not m.get('free'), m['name'].lower()))}

    async def add(self, uid, provider, requested):
        async with self.lock(uid):
            found = await self.catalog(uid, provider)
            catalog = {m['model']: m for m in found['models']}
            if any(m not in catalog for m in requested):
                raise RouterError('A selected model is no longer available. Refresh the list and try again.')
            router = await self.router_for(uid)
            key = await self.key(uid, router)
            known = {s['id'] for s in self.store.list_skills()}
            created = []
            for model in dict.fromkeys(requested):
                m = catalog[model]
                if m['added']:
                    continue
                created.append(self.store.save_connection(Connection(
                    name=f"{found['name']} · {m['name']}"[:80], model=model, base_url=router.url + '/v1', api_key=key,
                    free=bool(m.get('free')), context_window=m['context_window'], tier=m['tier'], reasoning=m['reasoning'],
                    skills=[s for s in m['skills'] if s in known], tools=m.get('tools'),
                    input_price=m.get('input_price'), output_price=m.get('output_price')).model_dump(), user_id=uid))
            return {'created': created}

    async def migrate(self, uid):
        """Move supported direct providers to 9Router, retaining model IDs, settings, and usage in L.E.O."""
        async with self.lock(uid):
            mine = self.store.list_connections(uid, True)
            direct = {}
            for c in mine:
                if c.get('local'):
                    continue
                host = urlsplit(c['base_url']).hostname
                provider = next((p for p, spec in KEY_PROVIDERS.items() if spec['host'] == host), None)
                if provider:
                    direct.setdefault(provider, []).append(c)
            moved = 0
            for provider, connections in direct.items():
                source = next((c for c in connections if c.get('secret')), None)
                if not source:
                    continue
                # Never silently collapse different credentials into a single router connection.
                keys = {self.store.decrypt(c) for c in connections if c.get('secret')}
                if len(keys) != 1:
                    continue
                router = await self.router_for(uid, create=True)
                await router.connect_api_key(provider, self.store.decrypt(source))
                key = await self.key(uid, router)
                spec = KEY_PROVIDERS[provider]
                for c in connections:
                    # Earlier setup required a model just to store a key, producing a fake provider-named model.
                    if len(connections) > 1 and c['model'].lower() == provider and c['name'].lower() == provider:
                        self.store.delete_connection(c['id'], uid)
                        continue
                    data = {k: c[k] for k in Connection.model_fields if k in c}
                    data.update(base_url=router.url + '/v1', protocol='openai',
                                model=f"{spec['prefix']}/{c['model']}", api_key=key)
                    self.store.save_connection(Connection(**data).model_dump(), c['id'], user_id=uid)
                    moved += 1
            return moved
