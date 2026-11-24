"""gomusicdl-service 契约测试：mock go-music-dl web 接口，验证搜索/详情/取流投影."""

import httpx
import pytest
from fastapi.testclient import TestClient

import app as svc


GO_SONG = {
    "id": "5257138",
    "name": "屋顶",
    "artist": "周杰伦、温岚",
    "album": "男女情歌对唱冠军全记录",
    "album_id": "abc",
    "duration": 319,
    "size": 12764517,
    "bitrate": 320,
    "source": "qq",
    "url": "",
    "ext": "mp3",
    "cover": "https://img.example/cover.jpg",
    "link": "https://y.qq.com/n/ryqq/songDetail/5257138",
    "extra": {"song_id": "5257138"},
}


class FakeRouter(httpx.AsyncClient):
    """按路径路由的假 go 服务。"""

    def __init__(self, handler, **kwargs):
        super().__init__(transport=httpx.MockTransport(handler), **kwargs)


@pytest.fixture()
def go_handler():
    """默认假 go 服务：搜索/inspect/download_lrc/download 全通。"""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/music/song_search":
            return httpx.Response(200, json={"ok": True, "songs": [GO_SONG], "errors": {}})
        if path == "/music/inspect":
            return httpx.Response(200, json={"valid": True, "url": "https://cdn.example/audio.mp3"})
        if path == "/music/download_lrc":
            return httpx.Response(200, text="[00:01.00] 屋顶")
        if path == "/music/download":
            return httpx.Response(200, content=b"AUDIO", headers={"content-type": "audio/mpeg"})
        return httpx.Response(404, json={"error": "not found"})
    return handler


@pytest.fixture(autouse=True)
def clean_caches():
    svc._SONG_CACHE.clear()
    svc._URL_CACHE.clear()
    svc._STATS.update({"searches": 0, "streams": 0, "info_hits": 0, "info_miss": 0, "resolve_fails": 0})
    yield
    svc._SONG_CACHE.clear()
    svc._URL_CACHE.clear()


@pytest.fixture()
def client(monkeypatch, go_handler):
    def fake_async_client(*args, **kwargs):
        return FakeRouter(go_handler, **kwargs)
    monkeypatch.setattr(svc.httpx, "AsyncClient", fake_async_client)
    with TestClient(svc.app) as c:
        yield c


def test_search_maps_items_into_contract(client):
    r = client.get("/search", params={"keyword": "屋顶", "limit": 5})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["engine"] == "gomusicdl"
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["id"] == "qq:5257138"
    assert item["source"] == "qq"
    assert item["title"] == "屋顶"
    assert item["duration_s"] == 319
    assert item["ext"] == "mp3"
    assert item["protocol"] == "http"
    assert item["delivery"] == "direct"
    assert item["quality"] == svc.UNKNOWN_QUALITY
    assert "download_url" not in item  # go 搜索未带 url 时不伪装直链
    assert "qq:5257138" in svc._SONG_CACHE


def test_search_with_url_includes_download_url(client, monkeypatch):
    song = dict(GO_SONG, url="https://cdn.example/direct.mp3")
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/music/song_search"
        return httpx.Response(200, json={"ok": True, "songs": [song], "errors": {}})
    monkeypatch.setattr(svc.httpx, "AsyncClient", lambda *a, **k: FakeRouter(handler, **k))
    r = client.get("/search", params={"keyword": "x"})
    item = r.json()["items"][0]
    assert item["download_url"] == "https://cdn.example/direct.mp3"


def test_search_source_errors_are_passed_through(client, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "songs": [], "errors": {"soda": "签名失败"}})
    monkeypatch.setattr(svc.httpx, "AsyncClient", lambda *a, **k: FakeRouter(handler, **k))
    r = client.get("/search", params={"keyword": "x"})
    assert r.json()["errors"]["soda"] == "签名失败"


def test_search_go_unreachable_reports_error(client, monkeypatch):
    def fake_async_client(*args, **kwargs):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(svc.httpx, "AsyncClient", fake_async_client)
    r = client.get("/search", params={"keyword": "x"})
    data = r.json()
    assert data["items"] == []
    assert "gomusicdl" in data["errors"]


def test_info_returns_contract_fields_and_lyric(client):
    client.get("/search", params={"keyword": "屋顶"})
    r = client.get("/info", params={"id": "qq:5257138"})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["id"] == "qq:5257138"
    assert data["title"] == "屋顶"
    assert data["lyric"] == "[00:01.00] 屋顶"
    assert data["cover_url"] == "https://img.example/cover.jpg"


def test_info_unknown_id_is_404(client):
    r = client.get("/info", params={"id": "qq:nope"})
    assert r.status_code == 404


def test_stream_proxy_true_streams_via_go_download(client):
    client.get("/search", params={"keyword": "屋顶"})
    r = client.get("/stream", params={"id": "qq:5257138", "proxy": "true"}, headers={"Range": "bytes=0-1"})
    assert r.status_code == 200
    assert r.content == b"AUDIO"
    assert r.headers["content-type"].startswith("audio/")


def test_stream_proxy_false_redirects_to_resolved_url(client):
    client.get("/search", params={"keyword": "屋顶"})
    r = client.get("/stream", params={"id": "qq:5257138", "proxy": "false"}, follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "https://cdn.example/audio.mp3"


def test_stream_proxy_false_falls_back_to_stream_when_resolve_fails(client, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/inspect":
            return httpx.Response(200, json={"valid": False, "reason": "resolution_failed"})
        if request.url.path == "/music/download":
            return httpx.Response(200, content=b"FALLBACK", headers={"content-type": "audio/mpeg"})
        if request.url.path == "/music/song_search":
            return httpx.Response(200, json={"ok": True, "songs": [GO_SONG], "errors": {}})
        return httpx.Response(404)
    monkeypatch.setattr(svc.httpx, "AsyncClient", lambda *a, **k: FakeRouter(handler, **k))
    client.get("/search", params={"keyword": "屋顶"})
    r = client.get("/stream", params={"id": "qq:5257138", "proxy": "false"}, follow_redirects=False)
    assert r.status_code == 200
    assert r.content == b"FALLBACK"


def test_stream_unknown_id_is_404(client):
    r = client.get("/stream", params={"id": "qq:nope", "proxy": "true"})
    assert r.status_code == 404


def test_invalid_song_id_rejected(client):
    # 缓存未命中即 404（畸形 id 与未知 id 同样处理，避免暴露内部解析细节）
    r = client.get("/info", params={"id": "invalid"})
    assert r.status_code == 404
    r = client.get("/stream", params={"id": "invalid", "proxy": "true"})
    assert r.status_code == 404


def test_healthz_reports_sources_and_cache(client):
    client.get("/search", params={"keyword": "屋顶"})
    r = client.get("/healthz")
    data = r.json()
    assert r.status_code == 200
    assert data["ok"] is True
    assert "qq" in data["sources"]
    assert data["cache_size"] >= 1
    assert data["stats"]["searches"] >= 1


def test_healthz_fails_when_go_backend_unreachable(monkeypatch):
    """go 进程在但 HTTP 失效时适配层必须报不健康（否则容器永远绿）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(svc.httpx, "AsyncClient", lambda *a, **k: FakeRouter(handler, **k))
    with TestClient(svc.app) as c:
        r = c.get("/healthz")
    data = r.json()
    assert r.status_code == 503
    assert data["ok"] is False
    assert data["go"]["ok"] is False
    assert "ConnectError" in data["go"]["detail"]


def test_healthz_fails_on_go_5xx(monkeypatch):
    monkeypatch.setattr(
        svc.httpx, "AsyncClient",
        lambda *a, **k: FakeRouter(lambda request: httpx.Response(502), **k))
    with TestClient(svc.app) as c:
        r = c.get("/healthz")
    assert r.status_code == 503
    assert r.json()["go"]["ok"] is False


def test_sources_do_not_hit_search_endpoint(client, monkeypatch):
    """状态页列源只做有界探活，不打真实搜索。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, text="<html></html>")

    monkeypatch.setattr(svc.httpx, "AsyncClient", lambda *a, **k: FakeRouter(handler, **k))
    r = client.get("/sources")
    data = r.json()
    assert r.status_code == 200
    assert data["go_ok"] is True
    assert data["registered"] == data["enabled"]
    assert "/music/song_search" not in seen
