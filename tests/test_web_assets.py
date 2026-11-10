"""HTTP cache contracts using temporary fixture bytes; no browser or rendering."""
import hashlib
import os
import re

import pytest
from fastapi.testclient import TestClient

from studio import app as module


@pytest.fixture
def web_client(tmp_path,monkeypatch):
    web=tmp_path/"web"
    web.mkdir()
    (web/"index.html").write_text('<!doctype html><link href="/styles.css"><link href="/booth.css"><script src="/app.js"></script>')
    for name,content in {"styles.css":b".view{color:red}","booth.css":b".booth{color:tan}","app.js":b"const version=1;"}.items():
        (web/name).write_bytes(content)
    monkeypatch.setattr(module,"ROOT",tmp_path)
    monkeypatch.setattr(module,"RUNS",{})
    # No lifespan or cloud clients: these tests exercise only local HTTP routes.
    return TestClient(module.app),web


def asset_urls(html):
    return dict(re.findall(r'(?:href|src)="/([^"?]+)\?v=([0-9a-f]{64})"',html))


@pytest.mark.parametrize("path",["/","/?view=booth","/index.html"])
def test_html_is_uncached_and_selects_content_versioned_assets(web_client,path):
    client,web=web_client
    response=client.get(path,headers={"If-None-Match":"*","If-Modified-Since":"Wed, 01 Jan 2031 00:00:00 GMT"})
    assert response.status_code==200
    assert response.headers["Cache-Control"]=="no-store"
    assert "etag" not in response.headers and "last-modified" not in response.headers
    versions=asset_urls(response.text)
    assert set(versions)==set(module.WEB_ASSETS)
    for name,digest in versions.items():
        expected=(web/name).read_bytes()
        assert digest==hashlib.sha256(expected).hexdigest()
        asset=client.get(f"/{name}?v={digest}")
        assert asset.status_code==200 and asset.content==expected
        assert asset.headers["ETag"]==f'"{digest}"'
        assert "immutable" in asset.headers["Cache-Control"]
        assert "last-modified" not in asset.headers
    head=client.head(path)
    assert head.status_code==200 and head.content==b""
    assert int(head.headers["Content-Length"])==len(response.content)


@pytest.mark.parametrize("name",["styles.css","booth.css","app.js"])
def test_same_size_same_mtime_edit_cannot_reuse_old_asset_or_html_version(web_client,name):
    client,web=web_client
    target=web/name
    before=client.get(f"/{name}")
    original_stat=target.stat()
    old_version=asset_urls(client.get("/").text)[name]
    changed={"styles.css":b".view{color:tan}","booth.css":b".booth{color:red}","app.js":b"const version=2;"}[name]
    assert changed!=before.content and len(changed)==len(before.content)
    target.write_bytes(changed)
    os.utime(target,ns=(original_stat.st_atime_ns,original_stat.st_mtime_ns))
    headers={"If-None-Match":before.headers["ETag"],"If-Modified-Since":"Wed, 01 Jan 2031 00:00:00 GMT"}
    after=client.get(f"/{name}",headers=headers)
    assert after.status_code==200 and after.content==changed
    assert after.headers["ETag"]!=before.headers["ETag"]
    # Date-only requests cannot override the content identity either.
    assert client.get(f"/{name}",headers={"If-Modified-Since":headers["If-Modified-Since"]}).status_code==200
    new_version=asset_urls(client.get("/").text)[name]
    assert new_version!=old_version
    stale=client.get(f"/{name}?v={old_version}",headers=headers)
    assert stale.status_code==404 and stale.headers["Cache-Control"]=="no-store"
    assert client.get(f"/{name}?v={new_version}").content==changed


@pytest.mark.parametrize("name",["styles.css","booth.css","app.js"])
def test_matching_content_validators_and_head_are_correct(web_client,name):
    client,web=web_client
    response=client.get(f"/{name}")
    assert response.headers["Cache-Control"]=="no-cache"
    etag=response.headers["ETag"]
    for condition in [etag,f"W/{etag}",f'"unrelated", {etag}',"*"]:
        cached=client.get(f"/{name}",headers={"If-None-Match":condition})
        assert cached.status_code==304 and cached.content==b""
        assert cached.headers["ETag"]==etag
        assert cached.headers["Cache-Control"]=="no-cache"
    head=client.head(f"/{name}")
    assert head.status_code==200 and head.content==b""
    assert int(head.headers["Content-Length"])==len((web/name).read_bytes())
    assert head.headers["ETag"]==etag


def test_asset_routes_preserve_api_mutation_and_preview_security(web_client):
    client,_=web_client
    assert client.get("/health").json()=={"status":"ready","service":"manager"}
    assert client.post("/api/runs",headers={"Origin":"https://external.invalid"},json={"prompt":"Apply a promotion"}).status_code==403
    module.RUNS["pending"]={"status":"running"}
    assert client.get("/preview/pending/").status_code==409
    module.RUNS["passed"]={"status":"passed","files":{"checkout.html":"<p>fixture</p>"}}
    preview=client.get("/preview/passed/")
    assert preview.status_code==200 and preview.headers["Cache-Control"]=="no-store"
    assert "sandbox allow-scripts" in preview.headers["Content-Security-Policy"]
    assert "allow-same-origin" not in preview.headers["Content-Security-Policy"]
    assert preview.headers["X-Content-Type-Options"]=="nosniff"
    assert preview.headers["Referrer-Policy"]=="no-referrer"
