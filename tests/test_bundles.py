import functools
import io
import tarfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from flatarmac import bundles, config
from flatarmac.config import Region

REGION = Region("testville", 2_600_000, 1_200_000, 2_601_000, 1_201_000, city="Testville")


@pytest.fixture
def release(tmp_path, monkeypatch):
    """A data dir to build into, a fresh one to install into, and a local release server."""
    built, fresh, assets = tmp_path / "built", tmp_path / "fresh", tmp_path / "assets"
    monkeypatch.setattr(config, "DATA_DIR", built)
    monkeypatch.setattr(bundles, "DATA_DIR", fresh)
    handler = functools.partial(SimpleHTTPRequestHandler, directory=assets)
    handler.log_message = lambda *args: None
    assets.mkdir()
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(bundles, "RELEASES", f"http://127.0.0.1:{server.server_port}")
    yield fresh, assets
    server.shutdown()


def test_bundle_round_trip(release):
    fresh, assets = release
    REGION.dir.mkdir(parents=True)
    for table in bundles.TABLES:
        (REGION.dir / table).write_bytes(table.encode())
    bundles.pack(REGION, assets)

    assert bundles.install("testville") == REGION
    for table in bundles.TABLES:
        assert (fresh / "testville" / table).read_bytes() == table.encode()
    assert "ODbL" in (fresh / "testville" / "ATTRIBUTION.txt").read_text()
    assert not list(fresh.glob(".*.part"))


def test_missing_region_says_so(release):
    with pytest.raises(LookupError):
        bundles.install("nowhere")


def test_bundle_must_stay_in_its_region_folder(release):
    _, assets = release
    with tarfile.open(assets / bundles.asset_name("testville"), "w:gz") as tar:
        info = tarfile.TarInfo("elsewhere/edges.parquet")
        tar.addfile(info, io.BytesIO())
    with pytest.raises(ValueError):
        bundles.install("testville")


def test_region_names_are_checked_before_downloading():
    with pytest.raises(ValueError):
        bundles.install("../etc")
