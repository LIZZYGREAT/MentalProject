from tests.test_admin_web import client, login


def test_participant_responses_do_not_expose_credentials_or_identity_ciphertext():
    browser = client()
    login(browser)
    responses = [
        browser.get("/admin/api/participants").text.lower(),
        browser.get("/admin/api/participants/P001").text.lower(),
    ]
    forbidden = (
        "student_no_ciphertext",
        "access_token_ciphertext",
        "refresh_token_ciphertext",
        "device_code_ciphertext",
        "feishu_app_secret",
        "deepseek_api_key",
        "token_encryption_key",
    )
    assert all(name not in text for text in responses for name in forbidden)


def test_refresh_requires_csrf_before_service_lookup():
    browser = client()
    session = login(browser)
    path = "/admin/api/participants/P001/forecasts/2030-01-15/refresh"
    assert browser.post(path, json={}).status_code == 401
    response = browser.post(
        path,
        json={},
        headers={"X-CSRF-Token": session["csrf_token"]},
    )
    assert response.status_code == 503
