from price_scanner.blocking import classify
from tests.conftest import fixture_html


def test_status_codes():
    assert classify(403, "https://x.example/", "<html></html>").state == "blocked"
    assert classify(429, "https://x.example/", "<html></html>").reason == "http_429"
    assert classify(200, "https://x.example/", "<html><title>Shop</title></html>").state == "ok"
    assert classify(500, "https://x.example/", "").state == "error"


def test_challenge_page_with_200():
    verdict = classify(200, "https://x.example/", fixture_html("blocked_datadome.html"))
    assert verdict.state == "blocked" and verdict.reason == "challenge_page"


def test_captcha_title():
    html = "<html><head><title>Are you a robot?</title></head></html>"
    assert classify(200, "https://x.example/", html).state == "blocked"


def test_login_wall():
    verdict = classify(200, "https://x.example/login/?next=/a", "<html></html>", login_url_patterns=["^/login"])
    assert verdict.state == "login-required"


def test_extra_markers():
    assert classify(200, "https://x.example/", "<p>Please verify you are human</p>",
                    extra_markers=["verify you are human"]).state == "blocked"
