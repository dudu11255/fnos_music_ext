"""Offline checks run with each service's real production dependencies installed."""
import argparse
import asyncio
import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def check_netease(app_dir: Path):
    verify_script = """
    const assert = require('assert');
    const { createService } = require('./service');
    const sdk = require('@neteasecloudmusicapienhanced/api');

    const requiredExports = [
      'cloudsearch', 'song_detail', 'song_url_v1', 'lyric_new',
      'artists', 'album', 'playlist_detail', 'login_status',
      'user_playlist', 'recommend_songs', 'toplist', 'playlist_catlist',
      'playlist_highquality_tags', 'personalized', 'top_playlist',
      'top_playlist_highquality', 'login_qr_key', 'login_qr_create',
      'login_qr_check', 'recommend_resource'
    ];
    for (const exp of requiredExports) {
      assert.strictEqual(typeof sdk[exp], 'function', 'Missing export: ' + exp);
    }

    const { app } = createService();
    const server = app.listen(0, '127.0.0.1', () => {
      const port = server.address().port;
      const http = require('http');
      http.get('http://127.0.0.1:' + port + '/healthz', (res) => {
        assert.strictEqual(res.statusCode, 200);
        let data = '';
        res.on('data', chunk => { data += chunk; });
        res.on('end', () => {
          const body = JSON.parse(data);
          assert.strictEqual(body.status, 'ok');
          assert.strictEqual(body.provider, 'netease');
          assert.strictEqual(body.upstream, '@neteasecloudmusicapienhanced/api');
          assert.strictEqual(body.version, '4.41.1');
          server.close(() => process.exit(0));
        });
      }).on('error', (err) => {
        console.error(err);
        process.exit(1);
      });
    });
    """
    proc = subprocess.run(
        ["node", "-e", verify_script],
        cwd=str(app_dir),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"NetEase production contract failed: {proc.stdout}\n{proc.stderr}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("service", choices=["musicdl", "netease", "lxmusic", "musicbox", "gomusicdl"])
    parser.add_argument("--app-dir", type=Path, required=True)
    parser.add_argument("--require-non-root", action="store_true")
    args = parser.parse_args()
    if args.require_non_root:
        assert os.geteuid() != 0, "Production service must run as non-root"
    with tempfile.TemporaryDirectory(prefix="fnmusic-contract-") as home:
        os.environ.update(HOME=home, XDG_CACHE_HOME=home + "/cache", XDG_DATA_HOME=home + "/data",
                          XDG_CONFIG_HOME=home + "/config", XDG_RUNTIME_DIR=home + "/run")
        for name in ("cache", "data", "config", "run"):
            Path(home, name).mkdir()
        if args.service in ("netease", "musicbox"):
            check_netease(args.app_dir)
        else:
            sys.path.insert(0, str(args.app_dir.resolve()))
            module = importlib.import_module("app")
            assert module.app is not None
            if args.service == "musicdl":
                from musicdl import musicdl
                import curl_cffi
                assert callable(musicdl.MusicClient)
                assert curl_cffi.requests.Session
            elif args.service == "gomusicdl":
                # 适配层契约：端点齐全；go 后端故意指向必然连不通的地址，
                # /healthz 必须如实报不健康（不得出现"进程在就永远绿"的假绿）。
                from fastapi.testclient import TestClient

                module.GO_URL = "http://127.0.0.1:9"
                paths = {route.path for route in module.app.routes}
                for path in ("/healthz", "/sources", "/search", "/info", "/stream"):
                    assert path in paths, f"缺少端点 {path}"
                with TestClient(module.app) as client:
                    resp = client.get("/healthz")
                assert resp.status_code == 503, resp.status_code
                assert resp.json().get("ok") is False
            else:
                from lxserver_client import SUPPORTED_PLATFORMS
                from verify_source import parse_script_meta
                assert "kw" in SUPPORTED_PLATFORMS
                meta = parse_script_meta(
                    "/*!\n * @name ci-src\n * @version 1.0.0\n * @author ci\n"
                    " * @description contract\n */\n"
                )
                assert meta["name"] == "ci-src"
        print(json.dumps({"service": args.service, "production_import": "pass", "offline_contract": "pass"}))


if __name__ == "__main__":
    main()
