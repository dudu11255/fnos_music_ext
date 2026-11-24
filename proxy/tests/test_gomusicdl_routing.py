"""gomusicdl（go-music-dl sidecar）接入代理的路由测试。

覆盖 review 关注的三件事：
- 启用判定：gomusicdl 自己声明平台时不受 musicdl 开关/平台白名单约束；
- 搜索：走适配层 /search，契约与 musicdl-service 一致并过滤不可播条目；
- 详情：命中 gomusicdl 时只打适配层，绝不回落到已停止的 musicdl。
"""

import httpx
import pytest
from fastapi import FastAPI, Request

from proxy import app as p


def make_request(path="/music/api/v1/track/stream"):
    return Request({
        "type": "http", "method": "GET", "path": path, "query_string": b"",
        "headers": [], "app": FastAPI(), "scheme": "http", "server": ("test", 80),
    })


@pytest.fixture()
def gomusicdl_on(monkeypatch):
    monkeypatch.setitem(p.CONF, "gomusicdl_enabled", True)
    monkeypatch.setitem(p.CONF, "gomusicdl_sources", "qq,bilibili,soda")
    # 切到 gomusicdl 后 musicdl 是停的，平台白名单也只留酷我
    monkeypatch.setitem(p.CONF, "musicdl_enabled", False)
    monkeypatch.setitem(p.CONF, "online_sources", "KuwoMusicClient")
    monkeypatch.setitem(p.CONF, "lx_enabled", False)
    monkeypatch.setitem(p.CONF, "netease_enabled", False)


def test_source_enabled_follows_gomusicdl_switch(gomusicdl_on, monkeypatch):
    assert p._source_enabled("online:qq:5257138") is True
    assert p._source_enabled("online:bilibili:BV1xx") is True
    # musicdl 未启用且平台白名单不含酷我 → 不可用
    assert p._source_enabled("online:kuwo:1") is False
    # 关掉 gomusicdl 后，同一曲目来源不再可用（musicdl 也没启用）
    monkeypatch.setitem(p.CONF, "gomusicdl_enabled", False)
    assert p._source_enabled("online:qq:5257138") is False


def test_empty_platform_config_falls_back_to_default_set(gomusicdl_on, monkeypatch):
    """启用但平台白名单为空（存量 .env 缺键）时按默认 qq,bilibili,soda 服务。"""
    monkeypatch.setitem(p.CONF, "gomusicdl_sources", "")
    assert p._gomusicdl_source_set() == {"qq", "bilibili", "soda"}
    assert p._source_enabled("online:qq:1") is True


@pytest.mark.anyio
async def test_online_info_routes_to_gomusicdl_only(gomusicdl_on):
    gomd_calls: list[tuple[str, dict]] = []
    musicdl_calls: list[str] = []

    def gomd_handler(req: httpx.Request) -> httpx.Response:
        gomd_calls.append((req.url.path, dict(req.url.params)))
        if req.url.path == "/info":
            return httpx.Response(200, json={"ok": True, "id": "qq:5257138", "title": "屋顶",
                                             "artist": "周杰伦", "ext": "mp3"})
        return httpx.Response(404)

    request = make_request()
    request.app.state.gomusicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(gomd_handler), base_url="http://gomd.test")
    request.app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: (musicdl_calls.append(req.url.path), httpx.Response(500))[1]),
        base_url="http://mdl.test")

    info = await p._fetch_online_info(request, "online:qq:5257138")
    assert info["title"] == "屋顶"
    assert gomd_calls == [("/info", {"id": "qq:5257138"})]
    assert musicdl_calls == []


@pytest.mark.anyio
async def test_online_info_gomusicdl_failure_does_not_fall_back_to_musicdl(gomusicdl_on):
    musicdl_calls: list[str] = []

    request = make_request()
    request.app.state.gomusicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(404, json={"detail": "miss"})),
        base_url="http://gomd.test")
    request.app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: (musicdl_calls.append(req.url.path), httpx.Response(500))[1]),
        base_url="http://mdl.test")

    assert await p._fetch_online_info(request, "online:qq:5257138") is None
    assert musicdl_calls == []


@pytest.mark.anyio
async def test_fetch_gomusicdl_search_passes_sources_and_filters_unplayable(gomusicdl_on):
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(dict(req.url.params))
        return httpx.Response(200, json={
            "ok": True,
            "items": [
                {"id": "qq:1", "source": "qq", "title": "屋顶", "duration_s": 200},
                {"id": "qq:2", "source": "qq", "title": "试听片段", "is_trial": True},
            ],
            "errors": {},
            "engine": "gomusicdl",
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://gomd.test")
    data = await p.fetch_gomusicdl_search(client, "屋顶", 5)

    assert data is not None
    # 默认把声明的平台全集合传给适配层（排序后逗号分隔，保证缓存键稳定）
    assert seen["sources"] == "bilibili,qq,soda"
    assert seen["keyword"] == "屋顶"
    assert [item["id"] for item in data["items"]] == ["qq:1"]


@pytest.mark.anyio
async def test_fetch_gomusicdl_search_returns_none_on_transport_error(gomusicdl_on):
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://gomd.test")
    assert await p.fetch_gomusicdl_search(client, "屋顶", 5) is None
