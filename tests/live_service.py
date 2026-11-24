from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import socket
from threading import Thread
import time

import httpx
import uvicorn

from guizhou_stay.api import create_app
from guizhou_stay.bundles import verify
from guizhou_stay.local_portal import create_app as partner_portal
from tests.support import evidence, policy
from tests.test_capture import AS_OF, FIXTURES


def main():
    with TemporaryDirectory() as directory, socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        address, port = listener.getsockname()
        app = create_app(Path(directory) / "stay.sqlite3")
        app.mount("/partner", partner_portal(FIXTURES))
        server = uvicorn.Server(uvicorn.Config(
            app, host=address, port=port, log_level="error", lifespan="off",
            timeout_graceful_shutdown=2,
        ))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 8
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert server.started
            base = f"http://{address}:{port}"
            with httpx.Client(base_url=base, timeout=6, trust_env=False) as client:
                assert client.get("/health").json() == {"status": "ready"}
                request = {"url": base + "/partner/offers", "limit": 1}
                partial = client.post("/collections/live-capture", params=request).raise_for_status().json()
                assert partial["next_token"] == "p2"
                assert client.get("/catalog").json()["items"] == []
                complete = client.post("/collections/live-capture", params=request).raise_for_status().json()
                assert complete["complete"]
                audit = client.get("/sessions/live-capture/audit").raise_for_status().json()
                assert audit["valid"] and all(value["has_raw"] for value in audit["pages"])
                rows = client.get("/catalog").raise_for_status().json()["items"]
                for row in rows:
                    proof = evidence("ev-" + row["item_id"], row["item_id"], row["category"])
                    proof["issued_at"] = "2026-11-19T00:00:00Z"
                    proof["expires_at"] = "2026-12-20T00:00:00Z"
                    client.post("/evidence", json=proof).raise_for_status()
                result = client.post("/packages", json={
                    "package_id": "demo-package", "region": "贵阳",
                    "policy": policy(), "requested_at": AS_OF, "expected_revision": 4,
                }).raise_for_status().json()
                archive = client.get("/packages/demo-package/archive").raise_for_status()
                assert verify(archive.content)["valid"]
                assert result["state"] == "published"
                outgoing = client.post("/deliveries", json={
                    "destination": "hk-agency", "delivery_id": "dispatch-one", "package_id": "demo-package",
                    "edition": 1, "expected_sequence": 0,
                }).raise_for_status().json()
                receipt = client.post("/deliveries/hk-agency/acknowledge", json={
                    "delivery_id": "dispatch-one", "receiver_reference": "agency-one",
                    "package_digest": result["digest"],
                }).raise_for_status().json()
                assert outgoing["package_digest"] == receipt["package_digest"] and receipt["acknowledged_at"]
                print("两页 HTML 断点采集、原始证据回放、供给包校验及渠道回执通过")
        finally:
            server.should_exit = True
            thread.join(5)
            if thread.is_alive():
                raise RuntimeError("验收服务未在时限内退出")


if __name__ == "__main__":
    main()
