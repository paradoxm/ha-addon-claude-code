"""What every caller of the API can rely on, whatever route they ask for."""

import pytest
from conftest import API_TOKEN


def test_liveness_probe_answers_without_a_token(client):
    answer = client.get("/ping", token=None)

    assert answer.status == 200
    assert answer.json == {"status": "ok"}


def test_everything_else_without_a_token_is_refused(client):
    answer = client.get("/health", token=None)

    assert answer.status == 401
    assert "token" in answer.json["error"]


def test_a_wrong_token_is_refused(client):
    answer = client.get("/health", token="not-the-configured-token")

    assert answer.status == 401


def test_a_token_that_is_a_prefix_of_the_real_one_is_refused(client):
    answer = client.get("/health", token=API_TOKEN[:-1])

    assert answer.status == 401


def test_a_refused_request_closes_the_connection(client):
    answer = client.get("/health", token="wrong")

    assert answer.headers.get("Connection", "").lower() == "close"


def test_an_unknown_path_answers_404_with_the_route_in_the_message(client):
    answer = client.get("/nothing-here")

    assert answer.status == 404
    assert "GET /nothing-here" in answer.json["error"]


def test_a_known_path_with_the_wrong_method_answers_404(client):
    answer = client.request("DELETE", "/health")

    assert answer.status == 404


def test_a_chunked_body_is_refused_instead_of_read_as_empty(client):
    answer = client.request(
        "PUT",
        "/settings",
        body=b"{}",
        headers={"Transfer-Encoding": "chunked"},
    )

    assert answer.status == 411
    assert "Content-Length" in answer.json["error"]


def test_a_negative_content_length_is_refused(client):
    answer = client.request("POST", "/jobs", body=b"x" * 50, headers={"Content-Length": "-1"})

    assert answer.status == 413


def test_a_content_length_that_is_not_a_number_is_refused(client):
    answer = client.request("POST", "/jobs", body=b"{}", headers={"Content-Length": "twelve"})

    assert answer.status == 400
    assert "Content-Length" in answer.json["error"]


def test_a_body_larger_than_the_upload_limit_is_refused_before_it_is_read(addon, client):
    oversized = str(addon.MAX_UPLOAD + 1)

    answer = client.request("POST", "/skills", headers={"Content-Length": oversized})

    assert answer.status == 413


def test_a_body_that_is_not_json_is_refused(client):
    answer = client.request("POST", "/jobs", body=b"{ truncated")

    assert answer.status == 400
    assert "invalid JSON" in answer.json["error"]


def test_a_json_body_that_is_not_an_object_is_refused(client):
    answer = client.send_json("POST", "/jobs", ["a", "list"])

    assert answer.status == 400
    assert "JSON object" in answer.json["error"]


def test_a_post_with_no_body_at_all_is_treated_as_an_empty_object(client):
    answer = client.request("POST", "/jobs")

    assert answer.status == 400
    assert answer.json["error"] == "'prompt' is required"


def test_an_unexpected_failure_answers_500_rather_than_dropping_the_connection(
    addon, client, monkeypatch
):
    def explode():
        raise RuntimeError("the disk went away")

    monkeypatch.setattr(addon, "list_skills", explode)

    answer = client.get("/skills")

    assert answer.status == 500
    assert answer.json["error"] == "RuntimeError: the disk went away"


def test_health_reports_the_choices_the_selectors_have_to_offer(addon, client):
    health = client.get("/health").json

    assert health["status"] == "ok"
    assert health["models"] == list(addon.MODEL_ALIASES)
    assert health["efforts"] == list(addon.EFFORTS)
    assert health["permission_modes"] == list(addon.PERMISSION_MODES)
    assert health["default_permission_mode"] in health["permission_modes"]
    assert health["default_model"] == "opus"
    assert health["default_effort"] == "medium"
    assert health["timeout_minutes"] == 5
    assert health["token_required"] is True


def test_health_reports_that_nobody_has_signed_in_yet(addon, client):
    credentials = addon.HOME / ".claude" / ".credentials.json"
    assert not credentials.exists()

    health = client.get("/health").json

    assert health["logged_in"] is False


def test_health_reports_a_sign_in_once_the_credentials_are_there(addon, client):
    credentials = addon.HOME / ".claude" / ".credentials.json"
    credentials.write_text("{}")
    try:
        health = client.get("/health").json
    finally:
        credentials.unlink()

    assert health["logged_in"] is True


@pytest.mark.parametrize("path", ["/skills/../etc", "/skills/%2e%2e/etc"])
def test_a_traversing_name_in_the_path_is_refused(client, path):
    answer = client.get(path)

    assert answer.status in (400, 404)
