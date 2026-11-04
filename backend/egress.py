"""Egress proxy for agent sandboxes: package registries only.

Sandboxes sit on an internal Docker network with no route out; this proxy is their only exit. It accepts HTTPS
CONNECT tunnels and plain-HTTP requests to allowlisted hosts (npm, PyPI, crates.io, the Go proxy, RubyGems, Maven
Central by default), and only when every address the host resolves to is public. Everything else gets a 403 that
tells the agent why. Run with `python -m backend.egress`.

LEO_EGRESS_ALLOW replaces the allowlist; LEO_EGRESS_ALLOW_EXTRA adds to it. An entry that starts with a dot
(".example.com") also allows its subdomains.
"""
import asyncio
import logging
import os
import socket

from .tools import _public_host

DEFAULT_ALLOW = ["registry.npmjs.org", "registry.yarnpkg.com", "pypi.org", "files.pythonhosted.org", "crates.io",
                 "index.crates.io", "static.crates.io", "proxy.golang.org", "sum.golang.org", "rubygems.org",
                 "index.rubygems.org", "repo.maven.apache.org", "repo1.maven.org"]
MAX_HEAD = 16384
IDLE_SECONDS = 300
logger = logging.getLogger("leo.egress")


def allowlist():
    base = os.getenv("LEO_EGRESS_ALLOW")
    hosts = [h for h in (base.split(",") if base is not None else DEFAULT_ALLOW)]
    hosts += os.getenv("LEO_EGRESS_ALLOW_EXTRA", "").split(",")
    return [h.strip().lower() for h in hosts if h.strip()]


def allowed(host, rules):
    host = host.lower().rstrip(".")
    return any(host == rule or (rule.startswith(".") and (host.endswith(rule) or host == rule[1:])) for rule in rules)


def parse_target(line):
    """(method, host, port, origin-form path) from a proxy request line, or None when it cannot be proxied."""
    parts = line.split(" ")
    if len(parts) != 3:
        return None
    method, target, _ = parts
    if method == "CONNECT":
        host, _, port = target.rpartition(":")
        return (method, host.strip("[]"), int(port), None) if port.isdigit() else None
    if not target.lower().startswith("http://"):
        return None
    rest = target[7:]
    authority, slash, path = rest.partition("/")
    host, _, port = authority.rpartition(":") if ":" in authority else (authority, "", "80")
    return (method, host, int(port), "/" + path if slash else "/") if port.isdigit() else None


async def pipe(reader, writer):
    try:
        while True:
            data = await asyncio.wait_for(reader.read(65536), IDLE_SECONDS)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (OSError, TimeoutError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


def denied(writer, reason):
    body = f"L.E.O. sandbox: {reason}. Sandboxes can only reach package registries; use the web_search and read_url tools for the web.\n"
    writer.write(f"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}".encode())


async def handle(reader, writer, rules, resolver=socket.getaddrinfo):
    upstream = None
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
        if len(head) > MAX_HEAD:
            return
        lines = head.decode("latin-1").split("\r\n")
        target = parse_target(lines[0])
        if not target:
            denied(writer, "unsupported proxy request")
            return
        method, host, port, path = target
        if port not in (80, 443) or not allowed(host, rules):
            logger.info("denied %s %s:%s", method, host, port)
            denied(writer, f"{host} is not on the package registry allowlist")
            return
        if not await asyncio.to_thread(_public_host, host, resolver):
            denied(writer, f"{host} does not resolve to a public address")
            return
        upstream_reader, upstream = await asyncio.wait_for(asyncio.open_connection(host, port), 15)
        if method == "CONNECT":
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
        else:
            # One request per connection, so a kept-alive client cannot reuse this tunnel for another host.
            headers = [h for h in lines[1:] if h and not h.lower().startswith(("proxy-", "connection:", "keep-alive:"))]
            upstream.write(("\r\n".join([f"{method} {path} HTTP/1.1", *headers, "Connection: close"]) + "\r\n\r\n").encode("latin-1"))
            await upstream.drain()
        logger.info("allowed %s %s:%s", method, host, port)
        await asyncio.gather(pipe(reader, upstream), pipe(upstream_reader, writer))
    except (OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, UnicodeDecodeError):
        pass
    finally:
        for stream in (writer, upstream):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass


async def serve(host="0.0.0.0", port=3128):
    rules = allowlist()
    server = await asyncio.start_server(lambda r, w: handle(r, w, rules), host, port, limit=MAX_HEAD)
    logger.info("Egress proxy on %s:%s allowing %s", host, port, ", ".join(rules))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(serve(port=int(os.getenv("LEO_EGRESS_PORT", "3128"))))
