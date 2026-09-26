import httpx
from fastapi.testclient import TestClient

from busapp import db
from busapp.mtd_rest import MtdRestClient
from busapp.realtime import FeedSnapshot
from busapp.stop_search import local_search, search_stops
from busapp.web import create_app

from .test_schedule_arrivals import seed


def rest_client(handler, key="k"):
    return MtdRestClient(api_key=key, base_url="https://api.test", transport=httpx.MockTransport(handler))


def test_local_search_matches_words_and_groups_platforms(conn):
    seed(conn)
    assert [r["id"] for r in local_search(conn, "illinois term")] == ["IT"]
    assert local_search(conn, "illinois term")[0]["name"] == "Illinois Terminal"
    assert [r["id"] for r in local_search(conn, "green goodwin")] == ["GWN"]
    assert [r["id"] for r in local_search(conn, "2")] == ["GWN"]      # stop code
    assert local_search(conn, "zzz") == []


def test_rest_search_used_and_sends_key(conn):
    seed(conn)
    seen = {}

    def handler(req):
        seen["key"] = req.headers.get("X-ApiKey")
        seen["q"] = req.url.params["query"]
        return httpx.Response(200, json={"result": [
            {"stopId": "IT", "name": "Illinois Terminal", "city": "Champaign"},
            {"stopId": "IT:1", "name": "Illinois Terminal", "city": "Champaign"},  # dedup to IT
            {"stopId": "UNKNOWN", "name": "Not in GTFS"}]})

    res = search_stops(conn, rest_client(handler), "illinois")
    assert seen == {"key": "k", "q": "illinois"}
    assert [(r["id"], r["source"]) for r in res] == [("IT", "mtd")]


def test_rest_rate_limit_backs_off_and_falls_back(conn):
    seed(conn)
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "3600"}, json={"result": None})

    rest = rest_client(handler)
    assert search_stops(conn, rest, "illinois")[0]["source"] == "local"
    assert not rest.enabled
    search_stops(conn, rest, "goodwin")
    assert len(calls) == 1  # no second call while blocked


def test_no_key_uses_local(conn):
    seed(conn)
    rest = rest_client(lambda req: (_ for _ in ()).throw(AssertionError("should not call")), key="")
    assert search_stops(conn, rest, "illinois")[0]["source"] == "local"


class StubRT:
    def get(self):
        return FeedSnapshot(0, {}), True


def test_web_endpoints(tmp_path):
    path = tmp_path / "t.db"
    c = db.connect(path)
    db.init_schema(c)
    seed(c)
    c.close()
    client = TestClient(create_app(path, rest=rest_client(lambda r: httpx.Response(500), key=""),
                                   rt_cache=StubRT()))
    r = client.get("/api/stops/search", params={"q": "illinois"})
    assert r.status_code == 200 and r.json()["results"][0]["id"] == "IT"
    r = client.get("/api/stops/IT/arrivals")
    assert r.status_code == 200
    body = r.json()
    assert body["stop"]["name"] == "Illinois Terminal" and isinstance(body["arrivals"], list)
    assert client.get("/api/stops/IT:1/arrivals").status_code == 200   # platform id accepted
    assert client.get("/api/stops/NOPE/arrivals").status_code == 404
    assert client.get("/api/stops/IT/arrivals", params={"model": "bogus"}).status_code == 400
    h = client.get("/api/health").json()
    assert h["observations"] == 0 and h["rest_search_enabled"] is False


def test_accuracy_endpoint(tmp_path):
    path = tmp_path / "t.db"
    c = db.connect(path)
    db.init_schema(c)
    c.close()
    client = TestClient(create_app(path, rest=rest_client(lambda r: httpx.Response(500), key=""),
                                   rt_cache=StubRT()))
    body = client.get("/api/accuracy").json()
    assert body["observations"] == 0 and len(body["by_horizon"]) == 6
    assert client.get("/api/accuracy", params={"model": "x"}).status_code == 400
