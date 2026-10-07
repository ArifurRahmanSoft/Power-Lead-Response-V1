from fastapi.testclient import TestClient


def test_local_angular_origin_is_allowed(client: TestClient) -> None:
    response = client.options(
        "/api/auth/register",
        headers={
            "Origin": "http://localhost:4200",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:4200"
    assert response.headers["access-control-allow-credentials"] == "true"
