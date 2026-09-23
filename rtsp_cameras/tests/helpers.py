"""Small helpers shared by the add-on tests."""

from __future__ import annotations

from starlette.testclient import TestClient

DEFAULT_URL = "rtsp://user:pass@192.168.1.10:554/stream1"


def add_camera(
    test_client: TestClient,
    name: str = "Front door",
    url: str = DEFAULT_URL,
    **extra: object,
) -> dict:
    """Create a camera through the API and return its payload."""
    body: dict = {"name": name, "url": url}
    body.update(extra)
    response = test_client.post("/api/cameras", json=body)
    assert response.status_code == 200, response.text
    return response.json()["camera"]
