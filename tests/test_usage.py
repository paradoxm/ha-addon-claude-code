"""How much of the plan's allowance is left, for a caller that would rather wait."""

import json
import time
import urllib.error

import pytest

PAYLOAD = {
    "five_hour": {"utilization": 96.0, "resets_at": "2026-08-12T20:40:00+00:00"},
    "seven_day": {"utilization": 78.0, "resets_at": "2026-08-13T03:00:00+00:00"},
    "seven_day_opus": None,
}


@pytest.fixture(autouse=True)
def forget_what_was_read(addon):
    """The answer is cached for a minute, which would hide the next test's setup."""

    def clear():
        addon.USAGE_CACHE.update(checked_at=float("-inf"), value=None)

    clear()
    yield
    clear()


@pytest.fixture
def signed_in(addon):
    addon.CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    addon.CREDENTIALS_PATH.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok"}}))
    yield
    addon.CREDENTIALS_PATH.unlink(missing_ok=True)


def answers_with(addon, monkeypatch, payload):
    class Answer:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    seen = {}

    def fake_open(request, timeout=None):
        seen["url"] = request.full_url
        seen["headers"] = dict(request.headers)
        return Answer()

    monkeypatch.setattr(addon.urllib.request, "urlopen", fake_open)
    return seen


def test_both_windows_are_reported_with_the_one_that_bites_first(
    addon, client, monkeypatch, signed_in
):
    answers_with(addon, monkeypatch, PAYLOAD)

    usage = client.get("/usage").json

    assert usage["available"] is True
    assert usage["session"] == {"percent": 96.0, "resets_at": "2026-08-12T20:40:00+00:00",
                                "threshold": addon.SESSION_THRESHOLD}
    assert usage["week"] == {"percent": 78.0, "resets_at": "2026-08-13T03:00:00+00:00",
                             "threshold": addon.WEEK_THRESHOLD}
    assert usage["worst"]["kind"] == "session"
    assert usage["worst"]["percent"] == 96.0


def test_the_reading_answers_whether_work_may_start_at_all(
    addon, client, monkeypatch, signed_in
):
    """One number in one place: a caller watching a long run should not carry its own copy
    of what "too full" means."""
    answers_with(addon, monkeypatch, PAYLOAD)

    usage = client.get("/usage").json

    assert usage["thresholds"] == {"session": addon.SESSION_THRESHOLD,
                                   "week": addon.WEEK_THRESHOLD}
    assert usage["enough"] is False, "ninety-six per cent of a window is not enough to start on"


def test_a_window_below_the_threshold_is_enough_to_work_on(
    addon, client, monkeypatch, signed_in
):
    answers_with(addon, monkeypatch, {
        "five_hour": {"utilization": 12, "resets_at": "2026-08-12T20:40:00Z"},
        "seven_day": {"utilization": 40, "resets_at": "2026-08-13T03:00:00Z"},
    })

    usage = client.get("/usage").json

    assert usage["enough"] is True
    assert usage["worst"]["percent"] == 40.0


def test_the_threshold_is_the_one_set_in_the_options(addon, client, monkeypatch, signed_in):
    monkeypatch.setattr(addon, "SESSION_THRESHOLD", 30)
    answers_with(addon, monkeypatch, {
        "five_hour": {"utilization": 40, "resets_at": "2026-08-12T20:40:00Z"},
        "seven_day": {"utilization": 10, "resets_at": "2026-08-13T03:00:00Z"},
    })

    usage = client.get("/usage").json

    assert usage["session"]["threshold"] == 30
    assert usage["enough"] is False, "forty per cent is over a threshold of thirty"


def test_the_account_s_own_credentials_are_what_is_asked_with(
    addon, client, monkeypatch, signed_in
):
    seen = answers_with(addon, monkeypatch, PAYLOAD)

    client.get("/usage")

    assert seen["url"] == addon.USAGE_URL
    assert seen["headers"]["Authorization"] == "Bearer tok"


def test_the_answer_is_kept_for_a_minute_rather_than_asked_on_every_poll(
    addon, client, monkeypatch, signed_in
):
    calls = []

    class Answer:
        def read(self):
            calls.append(1)
            return json.dumps(PAYLOAD).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(addon.urllib.request, "urlopen", lambda *a, **k: Answer())

    first = client.get("/usage").json
    second = client.get("/usage").json

    assert first == second
    assert len(calls) == 1

    client.get("/usage?refresh")

    assert len(calls) == 2


def test_nobody_signed_in_means_unavailable_rather_than_an_error(addon, client):
    addon.CREDENTIALS_PATH.unlink(missing_ok=True)

    usage = client.get("/usage").json

    assert usage["available"] is False
    assert usage["reason"] == "not signed in"


def test_an_endpoint_that_cannot_be_reached_does_not_become_a_failure(
    addon, client, monkeypatch, signed_in
):
    def unreachable(*_args, **_kwargs):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(addon.urllib.request, "urlopen", unreachable)

    usage = client.get("/usage").json

    assert usage["available"] is False
    assert "URLError" in usage["reason"]


def test_an_answer_in_a_shape_we_do_not_know_is_reported_as_unavailable(
    addon, client, monkeypatch, signed_in
):
    answers_with(addon, monkeypatch, {"something_else": {"utilization": 10}})

    usage = client.get("/usage").json

    assert usage["available"] is False
    assert usage["reason"] == "no windows reported"


def test_a_window_without_a_number_is_ignored(addon, client, monkeypatch, signed_in):
    answers_with(addon, monkeypatch, {
        "five_hour": {"resets_at": "2026-08-12T20:40:00+00:00"},
        "seven_day": PAYLOAD["seven_day"],
    })

    usage = client.get("/usage").json

    assert usage["session"] is None
    assert usage["worst"]["kind"] == "week"


def test_the_reading_says_whether_anything_is_done_about_it(addon, client, monkeypatch, signed_in):
    """Nobody should have to say "work is held" about an add-on that only reports."""
    answers_with(addon, monkeypatch, PAYLOAD)
    monkeypatch.setattr(addon, "GUARD_LIMITS", False)

    assert client.get("/usage").json["acting"] is False


def test_the_reading_says_which_clock_its_times_are_on(addon, client, monkeypatch, signed_in):
    """So a caller showing them to a person does not keep its own copy of where we are."""
    answers_with(addon, monkeypatch, PAYLOAD)
    monkeypatch.setitem(addon.TIMEZONE_CACHE, "value", "Asia/Yekaterinburg")

    assert client.get("/usage").json["timezone"] == "Asia/Yekaterinburg"


def refuses_with(addon, monkeypatch, code, headers=None):
    def refuse(*_args, **_kwargs):
        raise addon.urllib.error.HTTPError(
            addon.USAGE_URL, code, "Too Many Requests", headers or {}, None
        )

    monkeypatch.setattr(addon.urllib.request, "urlopen", refuse)


def test_asked_too_often_the_add_on_stops_asking_for_a_while(addon, client, monkeypatch, signed_in):
    """The endpoint says 429 when it has had enough, and asking again immediately is how a
    refusal turns into a ban."""
    monkeypatch.setitem(addon.USAGE_BACKOFF, "until", 0.0)
    addon.USAGE_CACHE.update(checked_at=float("-inf"), value=None)
    refuses_with(addon, monkeypatch, 429)

    answer = client.get("/usage").json

    assert answer["available"] is False
    assert "asked too often" in answer["reason"]
    assert answer["retry_at"], "and it says when it will try again"
    assert addon.USAGE_BACKOFF["until"] > addon.time.monotonic()


def test_pressing_refresh_cannot_shorten_that_wait(addon, client, monkeypatch, signed_in):
    """The button being pressed is exactly how the wall was hit."""
    monkeypatch.setitem(addon.USAGE_BACKOFF, "until", 0.0)
    addon.USAGE_CACHE.update(checked_at=float("-inf"), value=None)
    refuses_with(addon, monkeypatch, 429)
    client.get("/usage")

    asked = []
    monkeypatch.setattr(addon.urllib.request, "urlopen",
                        lambda *a, **k: asked.append(1) or (_ for _ in ()).throw(OSError("nope")))

    again = client.get("/usage?refresh").json

    assert asked == [], "not asked again, however hard the button is pressed"
    assert "asked too often" in again["reason"]


def test_its_own_retry_after_is_honoured_when_it_is_longer(addon, client, monkeypatch, signed_in):
    monkeypatch.setitem(addon.USAGE_BACKOFF, "until", 0.0)
    addon.USAGE_CACHE.update(checked_at=float("-inf"), value=None)
    refuses_with(addon, monkeypatch, 429, {"Retry-After": str(2 * 60 * 60)})

    client.get("/usage")

    assert addon.USAGE_BACKOFF["until"] - addon.time.monotonic() > addon.BACKOFF_SEC


def test_a_reading_that_works_again_clears_the_wait(addon, client, monkeypatch, signed_in):
    monkeypatch.setitem(addon.USAGE_BACKOFF, "until", 0.0)
    addon.USAGE_CACHE.update(checked_at=float("-inf"), value=None)
    answers_with(addon, monkeypatch, PAYLOAD)

    assert client.get("/usage").json["available"] is True
    assert addon.USAGE_BACKOFF["until"] == 0.0


def test_another_refusal_is_reported_as_itself(addon, client, monkeypatch, signed_in):
    monkeypatch.setitem(addon.USAGE_BACKOFF, "until", 0.0)
    addon.USAGE_CACHE.update(checked_at=float("-inf"), value=None)
    refuses_with(addon, monkeypatch, 503)

    answer = client.get("/usage").json

    assert answer["reason"] == "HTTP 503"
    assert "retry_at" not in answer
    assert addon.USAGE_BACKOFF["until"] == 0.0, "and does not stop the next read"


def test_a_retry_after_that_is_not_a_number_is_ignored(addon, client, monkeypatch, signed_in):
    """Some proxies write it as a date, and int() on that is not a reason to fail a read."""
    monkeypatch.setitem(addon.USAGE_BACKOFF, "until", 0.0)
    addon.USAGE_CACHE.update(checked_at=float("-inf"), value=None)
    refuses_with(addon, monkeypatch, 429, {"Retry-After": "Wed, 13 Aug 2026 21:00:00 GMT"})

    answer = client.get("/usage").json

    assert "asked too often" in answer["reason"]
    assert addon.USAGE_BACKOFF["until"] - addon.time.monotonic() > 0


# --------------------------------------------------------------------------- #
# the token behind the reading
# --------------------------------------------------------------------------- #
# An access token lives about eight hours. The CLI renews it from the refresh token
# beside it, but only while the CLI runs — so an add-on left alone overnight woke to a
# spent token, read 401 for the rest of its life, and took the guard blind with it.


class Reply:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def two_endpoints(addon, monkeypatch, *, accepted=("renewed",), renewal=None, usage=PAYLOAD):
    """Both endpoints a reading may touch: the allowance, and the renewal behind it.

    The allowance is handed only to a token in `accepted`; any other gets the 401 a spent
    token really gets. `renewal=None` means the renewal itself is refused.
    """
    seen = {"asked": [], "renewals": 0, "body": None}

    def fake_open(request, timeout=None):
        if request.full_url == addon.TOKEN_URL:
            seen["renewals"] += 1
            seen["body"] = json.loads(request.data)
            if renewal is None:
                raise urllib.error.HTTPError(
                    addon.TOKEN_URL, 400, "invalid_grant", {}, None
                )
            return Reply(renewal)
        token = request.headers["Authorization"].removeprefix("Bearer ")
        seen["asked"].append(token)
        if token not in accepted:
            raise urllib.error.HTTPError(addon.USAGE_URL, 401, "Unauthorized", {}, None)
        return Reply(usage)

    monkeypatch.setattr(addon.urllib.request, "urlopen", fake_open)
    return seen


def sign_in(addon, **oauth):
    addon.CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    addon.CREDENTIALS_PATH.write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": "spent",
            "refreshToken": "renew-me",
            "scopes": ["user:inference", "user:profile"],
            **oauth,
        },
        # Somebody else's tokens live in the same file, and they are not ours to lose.
        "mcpOAuth": {"sentry|abc": {"accessToken": "not ours"}},
    }))


def stored_oauth(addon) -> dict:
    return json.loads(addon.CREDENTIALS_PATH.read_text())["claudeAiOauth"]


@pytest.fixture
def signed_in_yesterday(addon):
    """Signed in, and the token died in the night: what an idle add-on wakes up to."""
    sign_in(addon, expiresAt=int((time.time() - 3600) * 1000))
    yield
    addon.CREDENTIALS_PATH.unlink(missing_ok=True)


def test_a_token_that_died_overnight_is_renewed_before_the_reading(
    addon, client, monkeypatch, signed_in_yesterday
):
    seen = two_endpoints(addon, monkeypatch, renewal={"access_token": "renewed",
                                                      "expires_in": 8 * 3600})

    usage = client.get("/usage").json

    assert usage["available"] is True, "a dead token is a thing to renew, not to report"
    assert seen["asked"] == ["renewed"], "and the spent one is not tried first"
    assert stored_oauth(addon)["accessToken"] == "renewed"
    assert stored_oauth(addon)["expiresAt"] / 1000 > time.time() + 3600


def test_a_token_about_to_die_is_renewed_before_it_does(
    addon, client, monkeypatch, signed_in_yesterday
):
    """Otherwise the renewal costs a reading, and a reading is the thing being rationed."""
    sign_in(addon, expiresAt=int((time.time() + 60) * 1000))
    seen = two_endpoints(addon, monkeypatch, renewal={"access_token": "renewed",
                                                      "expires_in": 8 * 3600})

    assert client.get("/usage").json["available"] is True
    assert seen["renewals"] == 1


def test_the_renewal_asks_the_way_the_cli_asks(
    addon, client, monkeypatch, signed_in_yesterday
):
    seen = two_endpoints(addon, monkeypatch, renewal={"access_token": "renewed",
                                                      "expires_in": 8 * 3600})

    client.get("/usage")

    assert seen["body"] == {
        "grant_type": "refresh_token",
        "refresh_token": "renew-me",
        "client_id": addon.OAUTH_CLIENT_ID,
        "scope": "user:inference user:profile",
    }


def test_a_client_id_recorded_in_the_file_is_preferred_to_our_own(
    addon, client, monkeypatch, signed_in_yesterday
):
    """The file is the account's own truth; the constant is only a last resort."""
    sign_in(addon, expiresAt=0, clientId="the-file-s-own-client")
    seen = two_endpoints(addon, monkeypatch, renewal={"access_token": "renewed",
                                                      "expires_in": 3600})

    client.get("/usage")

    assert seen["body"]["client_id"] == "the-file-s-own-client"


def test_a_401_on_a_token_that_looked_good_is_renewed_and_asked_again(
    addon, client, monkeypatch, signed_in_yesterday
):
    """The way the CLI answers the same 401: renew, ask again, and only then give up."""
    sign_in(addon, accessToken="looks-fine", expiresAt=int((time.time() + 8 * 3600) * 1000))
    seen = two_endpoints(addon, monkeypatch, renewal={"access_token": "renewed",
                                                      "expires_in": 8 * 3600})

    usage = client.get("/usage").json

    assert usage["available"] is True
    assert seen["asked"] == ["looks-fine", "renewed"]
    assert seen["renewals"] == 1


def test_a_renewal_that_is_refused_says_the_sign_in_has_expired(
    addon, client, monkeypatch, signed_in_yesterday
):
    seen = two_endpoints(addon, monkeypatch, renewal=None)

    usage = client.get("/usage").json

    assert usage["available"] is False
    assert usage["reason"] == "the sign-in has expired"
    assert seen["renewals"] == 1, "and the refusal is not argued with"
    assert seen["asked"] == [], "nor is a dead token spent on a request that must fail"
    assert stored_oauth(addon)["refreshToken"] == "renew-me", "a refusal is not a logout"


def test_a_new_refresh_token_replaces_the_spent_one(
    addon, client, monkeypatch, signed_in_yesterday
):
    """A renewal may rotate it, and keeping the old one would make the next renewal fail."""
    two_endpoints(addon, monkeypatch, renewal={"access_token": "renewed", "expires_in": 3600,
                                               "refresh_token": "the-next-one"})

    client.get("/usage")

    assert stored_oauth(addon)["refreshToken"] == "the-next-one"


def test_a_renewal_without_a_new_refresh_token_keeps_the_old_one(
    addon, client, monkeypatch, signed_in_yesterday
):
    two_endpoints(addon, monkeypatch, renewal={"access_token": "renewed", "expires_in": 3600})

    client.get("/usage")

    assert stored_oauth(addon)["refreshToken"] == "renew-me"


def test_scopes_the_renewal_reports_are_kept(addon, client, monkeypatch, signed_in_yesterday):
    two_endpoints(addon, monkeypatch, renewal={"access_token": "renewed", "expires_in": 3600,
                                               "scope": "user:inference"})

    client.get("/usage")

    assert stored_oauth(addon)["scopes"] == ["user:inference"]


def test_nothing_else_in_the_credentials_file_is_touched(
    addon, client, monkeypatch, signed_in_yesterday
):
    two_endpoints(addon, monkeypatch, renewal={"access_token": "renewed", "expires_in": 3600})

    client.get("/usage")

    whole = json.loads(addon.CREDENTIALS_PATH.read_text())
    assert whole["mcpOAuth"] == {"sentry|abc": {"accessToken": "not ours"}}
    assert addon.CREDENTIALS_PATH.stat().st_mode & 0o777 == 0o600, "and nobody else can read it"


def test_nothing_is_renewed_while_the_cli_is_running(
    addon, client, monkeypatch, signed_in_yesterday
):
    """A running turn renews its own token, and two renewals spend the refresh token twice —
    which signs the account out rather than renewing it."""
    monkeypatch.setattr(addon, "RUNNING_PROC", object())
    seen = two_endpoints(addon, monkeypatch, renewal={"access_token": "renewed",
                                                      "expires_in": 3600})

    usage = client.get("/usage").json

    assert seen["renewals"] == 0
    assert usage["available"] is False
    assert usage["reason"] == "HTTP 401"
    assert stored_oauth(addon)["accessToken"] == "spent", "the file is the CLI's to write"


def test_a_token_with_no_expiry_recorded_is_used_as_it_is(
    addon, client, monkeypatch, signed_in_yesterday
):
    """Nothing can be said about it, so a 401 is what settles the question."""
    sign_in(addon, accessToken="renewed")
    seen = two_endpoints(addon, monkeypatch)

    assert client.get("/usage").json["available"] is True
    assert seen["renewals"] == 0


def test_a_file_with_no_refresh_token_in_it_cannot_renew(
    addon, client, monkeypatch, signed_in_yesterday
):
    addon.CREDENTIALS_PATH.write_text(json.dumps({
        "claudeAiOauth": {"accessToken": "spent", "expiresAt": 0}
    }))
    seen = two_endpoints(addon, monkeypatch, renewal={"access_token": "renewed",
                                                      "expires_in": 3600})

    usage = client.get("/usage").json

    assert seen["renewals"] == 0, "there is nothing to renew with"
    assert seen["asked"] == [], "and a token known to be dead is not worth a request"
    assert usage["available"] is False
    assert usage["reason"] == "the sign-in has expired"


def test_a_renewal_answer_that_makes_no_sense_is_not_written_back(
    addon, client, monkeypatch, signed_in_yesterday
):
    two_endpoints(addon, monkeypatch, renewal={"expires_in": 3600})

    usage = client.get("/usage").json

    assert usage["reason"] == "the sign-in has expired"
    assert stored_oauth(addon)["accessToken"] == "spent"


def test_a_token_that_could_not_be_saved_is_still_used(
    addon, client, monkeypatch, signed_in_yesterday
):
    """A read-only volume is a reason to renew again next time, not to stop reading."""
    two_endpoints(addon, monkeypatch, renewal={"access_token": "renewed", "expires_in": 3600})
    monkeypatch.setattr(addon, "write_credentials",
                        lambda _stored: (_ for _ in ()).throw(OSError("read-only")))

    assert client.get("/usage").json["available"] is True


def test_a_401_that_survives_the_renewal_says_the_sign_in_has_expired(
    addon, client, monkeypatch, signed_in_yesterday
):
    """The file said the token had hours left, the endpoint disagreed, and the refresh token
    is spent too: nothing left to do but say so."""
    sign_in(addon, accessToken="looks-fine", expiresAt=int((time.time() + 8 * 3600) * 1000))
    seen = two_endpoints(addon, monkeypatch, renewal=None)

    usage = client.get("/usage").json

    assert seen["asked"] == ["looks-fine"], "and it is not asked a third time"
    assert usage["available"] is False
    assert usage["reason"] == "the sign-in has expired"


def test_a_renewal_answering_something_that_is_not_a_token_is_refused(
    addon, client, monkeypatch, signed_in_yesterday
):
    two_endpoints(addon, monkeypatch, renewal={"access_token": 12345, "expires_in": 3600})

    assert client.get("/usage").json["reason"] == "the sign-in has expired"
    assert stored_oauth(addon)["accessToken"] == "spent"


def test_a_sign_in_with_no_token_in_it_is_no_sign_in(addon, client, monkeypatch):
    """Half a credentials file is what a failed login leaves behind."""
    addon.CREDENTIALS_PATH.write_text(json.dumps({"claudeAiOauth": {"scopes": []}}))
    seen = two_endpoints(addon, monkeypatch, renewal={"access_token": "renewed",
                                                      "expires_in": 3600})

    usage = client.get("/usage").json

    assert usage["reason"] == "not signed in"
    assert seen["renewals"] == 0 and seen["asked"] == []


def test_a_credentials_file_in_another_shape_is_no_sign_in(addon, client, monkeypatch):
    """It is written by something else; a list where a map belongs is not worth a 500."""
    addon.CREDENTIALS_PATH.write_text("[]")

    assert client.get("/usage").json["reason"] == "not signed in"


def test_a_renewal_that_does_not_say_how_long_is_not_repeated_on_every_reading(
    addon, client, monkeypatch, signed_in_yesterday
):
    """Keeping yesterday's expiry beside today's token would renew again three minutes later."""
    seen = two_endpoints(addon, monkeypatch, renewal={"access_token": "renewed"})

    assert client.get("/usage").json["available"] is True
    assert "expiresAt" not in stored_oauth(addon)

    addon.USAGE_CACHE.update(checked_at=float("-inf"), value=None)

    assert client.get("/usage").json["available"] is True
    assert seen["renewals"] == 1


def test_a_turn_that_got_there_first_stops_the_renewal_dead(
    addon, monkeypatch, signed_in_yesterday
):
    """The check inside the lock, which is what makes «never while the CLI runs» a fact
    rather than a hope: a reading can decide to renew a moment before a turn starts, and
    whoever takes the lock first wins."""
    monkeypatch.setattr(addon, "RUNNING_PROC", object())
    seen = two_endpoints(addon, monkeypatch, renewal={"access_token": "renewed",
                                                      "expires_in": 3600})

    assert addon.renew_access_token() is None
    assert seen["renewals"] == 0
    assert stored_oauth(addon)["accessToken"] == "spent"


# --------------------------------------------------------------------------- #
# keeping the sign-in alive
# --------------------------------------------------------------------------- #
# Renewing on a 401 repairs the reading, but only once somebody looks. The token is kept
# warm on the watcher's own tick instead, so it never runs out in the first place.


@pytest.fixture(autouse=True)
def forget_the_last_renewal(addon):
    def clear():
        addon.TOKEN_KEEP.update(until=0.0, looked=float("-inf"))

    clear()
    yield
    clear()


def test_a_token_near_its_end_is_renewed_on_a_tick_of_its_own(
    addon, monkeypatch, signed_in_yesterday
):
    """Nobody has to be looking: this is the whole point of it."""
    sign_in(addon, expiresAt=int((time.time() + 30 * 60) * 1000))
    seen = two_endpoints(addon, monkeypatch, renewal={"access_token": "renewed",
                                                     "expires_in": 8 * 3600})

    addon.limit_watch_once()

    assert seen["renewals"] == 1
    assert seen["asked"] == [], "and the allowance is not read for it"
    assert stored_oauth(addon)["accessToken"] == "renewed"


def test_a_token_with_hours_left_is_left_alone(addon, monkeypatch, signed_in_yesterday):
    sign_in(addon, expiresAt=int((time.time() + 7 * 3600) * 1000))
    seen = two_endpoints(addon, monkeypatch, renewal={"access_token": "renewed",
                                                     "expires_in": 8 * 3600})

    addon.limit_watch_once()

    assert seen["renewals"] == 0


def test_the_sign_in_is_kept_warm_even_with_the_guard_switched_off(
    addon, monkeypatch, signed_in_yesterday
):
    """The sign-in is the account's, not the guard's: turning the guard off should not end
    with an add-on that has to be signed in again by hand."""
    monkeypatch.setattr(addon, "GUARD_LIMITS", False)
    sign_in(addon, expiresAt=int((time.time() + 30 * 60) * 1000))
    seen = two_endpoints(addon, monkeypatch, renewal={"access_token": "renewed",
                                                     "expires_in": 8 * 3600})

    addon.limit_watch_once()

    assert seen["renewals"] == 1


def test_nothing_is_kept_warm_while_a_turn_is_running(addon, monkeypatch, signed_in_yesterday):
    sign_in(addon, expiresAt=int((time.time() + 30 * 60) * 1000))
    monkeypatch.setattr(addon, "RUNNING_PROC", object())
    seen = two_endpoints(addon, monkeypatch, renewal={"access_token": "renewed",
                                                     "expires_in": 8 * 3600})

    addon.limit_watch_once()

    assert seen["renewals"] == 0, "the CLI renews its own while it runs"


def test_a_renewal_that_failed_is_not_tried_again_on_the_next_tick(
    addon, monkeypatch, signed_in_yesterday
):
    """Thirty seconds apart, for ever, is how a spent refresh token turns into a flood."""
    sign_in(addon, expiresAt=int((time.time() + 30 * 60) * 1000))
    seen = two_endpoints(addon, monkeypatch, renewal=None)

    addon.limit_watch_once()
    addon.limit_watch_once()

    assert seen["renewals"] == 1
    assert addon.TOKEN_KEEP["until"] > addon.time.monotonic()


def test_a_renewal_that_worked_clears_that_wait(addon, monkeypatch, signed_in_yesterday):
    sign_in(addon, expiresAt=int((time.time() + 30 * 60) * 1000))
    two_endpoints(addon, monkeypatch, renewal={"access_token": "renewed", "expires_in": 3600})

    addon.limit_watch_once()

    assert addon.TOKEN_KEEP["until"] == 0.0


def test_a_sign_in_with_no_expiry_recorded_is_not_touched_by_the_tick(
    addon, monkeypatch, signed_in_yesterday
):
    """Nothing is known about when it dies, so there is nothing to be early about."""
    sign_in(addon)
    seen = two_endpoints(addon, monkeypatch, renewal={"access_token": "renewed",
                                                     "expires_in": 3600})

    addon.limit_watch_once()

    assert seen["renewals"] == 0


def test_a_renewal_blowing_up_does_not_take_the_watcher_with_it(
    addon, monkeypatch, signed_in_yesterday
):
    """It is the thread that also freezes and thaws turns; it must outlive any one reading."""
    sign_in(addon, expiresAt=int((time.time() + 30 * 60) * 1000))
    monkeypatch.setattr(addon, "credentials",
                        lambda: (_ for _ in ()).throw(RuntimeError("nonsense")))

    assert addon.limit_watch_once() == addon.WATCH_IDLE_SEC


def test_the_token_s_clock_is_consulted_no_faster_than_the_setting(
    addon, monkeypatch, signed_in_yesterday
):
    """The watcher ticks every half minute so it can notice a turn starting and ending —
    nothing to do with Anthropic. Nothing hung on that tick may run at that pace, though:
    `usage_check_seconds` governs how often this add-on touches anything of theirs."""
    assert addon.USAGE_TTL_SEC >= 30, "the setting is the pace, and this test rests on it"
    sign_in(addon, expiresAt=int((time.time() + 30 * 60) * 1000))
    looks = []
    consult = addon.credentials
    monkeypatch.setattr(addon, "credentials", lambda: looks.append(1) or consult())
    # A renewal that hands back a minute-long token, so the next tick would renew again if
    # the pace were not kept to.
    seen = two_endpoints(addon, monkeypatch, renewal={"access_token": "renewed",
                                                     "expires_in": 60})

    addon.limit_watch_once()
    addon.limit_watch_once()
    addon.limit_watch_once()

    assert seen["renewals"] == 1, "three ticks inside one interval, one renewal"
    # Twice for the one tick that acted — the look, then the renewal's own read under the
    # lock — and not at all for the two that followed.
    assert looks == [1, 1], "the file is not read again either"


# --------------------------------------------------------------------------- #
# a figure for each window
# --------------------------------------------------------------------------- #
# The five-hour window refills as the day goes on; the week is what a fortnight of work has
# to fit inside. One figure for both meant being strict about the wrong one.


def test_each_window_is_judged_against_its_own_figure(addon, client, monkeypatch, signed_in):
    monkeypatch.setattr(addon, "SESSION_THRESHOLD", 95)
    monkeypatch.setattr(addon, "WEEK_THRESHOLD", 50)
    answers_with(addon, monkeypatch, {
        "five_hour": {"utilization": 80, "resets_at": "2026-08-12T20:40:00Z"},
        "seven_day": {"utilization": 60, "resets_at": "2026-08-13T03:00:00Z"},
    })

    usage = client.get("/usage").json

    assert usage["session"]["threshold"] == 95
    assert usage["week"]["threshold"] == 50
    assert usage["enough"] is False, "the week is past its own figure, and that is enough"
    assert usage["worst"]["kind"] == "week", "even though the session window is the fuller one"


def test_the_fuller_window_is_not_the_one_that_bites_first(
    addon, client, monkeypatch, signed_in
):
    """Which window stops work is a question of room left to its own figure, not of which
    number is bigger — that is the whole point of setting them apart."""
    monkeypatch.setattr(addon, "SESSION_THRESHOLD", 95)
    monkeypatch.setattr(addon, "WEEK_THRESHOLD", 75)
    answers_with(addon, monkeypatch, {
        "five_hour": {"utilization": 85, "resets_at": "2026-08-12T20:40:00Z"},  # ten to go
        "seven_day": {"utilization": 70, "resets_at": "2026-08-13T03:00:00Z"},  # five to go
    })

    usage = client.get("/usage").json

    assert usage["worst"]["kind"] == "week"
    assert usage["enough"] is True, "neither is over yet"


def test_a_session_over_its_figure_stops_work_though_the_week_is_empty(
    addon, client, monkeypatch, signed_in
):
    monkeypatch.setattr(addon, "SESSION_THRESHOLD", 60)
    monkeypatch.setattr(addon, "WEEK_THRESHOLD", 90)
    answers_with(addon, monkeypatch, {
        "five_hour": {"utilization": 65, "resets_at": "2026-08-12T20:40:00Z"},
        "seven_day": {"utilization": 5, "resets_at": "2026-08-13T03:00:00Z"},
    })

    usage = client.get("/usage").json

    assert usage["enough"] is False
    assert usage["worst"]["kind"] == "session"


def test_the_figures_are_reported_even_when_the_reading_cannot_be_had(addon, client):
    """The page draws where the guard stands whether or not the bars have anything in them."""
    addon.CREDENTIALS_PATH.unlink(missing_ok=True)

    usage = client.get("/usage").json

    assert usage["available"] is False
    assert usage["thresholds"] == {"session": addon.SESSION_THRESHOLD,
                                   "week": addon.WEEK_THRESHOLD}
