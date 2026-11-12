"""A loopback HTTP client for tests that drive the real API server with the session cookie."""

from __future__ import annotations

import http.client
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlite3 import Connection

    from akashic.api.server import ApiServer


class Client:
    def __init__(self, server: ApiServer, token: str, conn: Connection) -> None:
        self.server, self.token, self.conn = server, token, conn

    def call(self, path: str, method: str = "GET", body: object = None) -> tuple[int, Any]:
        request = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        headers = {"Cookie": f"akashic_session={self.token}"}
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        request.request(method, path, body=payload, headers=headers)
        response = request.getresponse()
        data = response.read()
        request.close()
        return response.status, json.loads(data)
