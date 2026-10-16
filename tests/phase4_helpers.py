import copy
import json
import threading

import httpx

from trading_bot.persistence.supabase_repository import SupabaseAPI


class MemoryRPC:
    """Transport test double only; actual SQL/RLS tests run separately on PostgreSQL."""

    def __init__(self):
        self.states = {}
        self.lock = threading.Lock()
        self.requests = []
        self.api = SupabaseAPI(
            "https://isolated.invalid",
            "sb_secret_test",
            httpx.Client(transport=httpx.MockTransport(self.handle)),
        )

    def handle(self, request):
        assert request.headers["apikey"] == "sb_secret_test"
        assert "Authorization" not in request.headers
        assert request.url.host == "isolated.invalid"
        params = json.loads(request.content)
        name = request.url.path.rsplit("/", 1)[-1]
        with self.lock:
            self.requests.append(name)
            key = params["p_account"]
            state = self.states.get(key, {"version": 0, "account": None, "records": []})
            if name == "bot_read":
                return httpx.Response(200, json=copy.deepcopy(state))
            if name == "bot_commit":
                if state["version"] != params["p_version"]:
                    return httpx.Response(409, json={"code": "40001", "message": "secret-body"})
                updated = copy.deepcopy(state)
                updated["version"] += 1
                updated["account"] = params["p_value"]
                for item in params["p_writes"]:
                    previous = next(
                        (
                            r
                            for r in updated["records"]
                            if (r["kind"], r["key"]) == (item["kind"], item["key"])
                        ),
                        None,
                    )
                    if previous:
                        updated["records"].remove(previous)
                    updated["records"].append(item)
                self.states[key] = updated
                return httpx.Response(200, json=updated["version"])
            raise AssertionError("unexpected RPC")
