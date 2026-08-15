// The flows that involve more than one request: installing a skill, saving the
// permissions file, coming back to an earlier conversation, renaming this one.

import assert from "node:assert/strict";
import test from "node:test";

import { bootConsole, CHAT, HEALTH } from "./console.mjs";

const SESSIONS = {
    sessions: [
        {
            id: "abcdef12-0000-4000-8000-000000000001",
            title: "The boiler",
            custom: true,
            preview: "look at the boiler log",
            messages: 8,
            updated_at: "2026-08-12T09:00:00+00:00",
        },
        {
            id: "abcdef12-0000-4000-8000-000000000002",
            title: "About the questionnaires",
            custom: false,
            preview: "read the two forms",
            messages: 2,
            updated_at: "2026-08-11T09:00:00+00:00",
        },
    ],
    current: "abcdef12-0000-4000-8000-000000000001",
};

test("the history sheet lists conversations and filters them as you type", async () => {
    const page = await bootConsole({ "chat/sessions": SESSIONS });

    await page.click(page.id("history"));

    assert.equal(page.id("history-overlay").hidden, false);
    assert.equal(page.document.querySelectorAll(".session").length, 2);

    page.id("history-search").value = "questionnaire";
    page.id("history-search").dispatchEvent(new page.window.Event("input", { bubbles: true }));
    await page.settle();

    const shown = [...page.document.querySelectorAll(".session")];
    assert.equal(shown.length, 1);
    assert.match(shown[0].textContent, /About the questionnaires/);

    page.id("history-search").value = "nothing matches this";
    page.id("history-search").dispatchEvent(new page.window.Event("input", { bubbles: true }));
    await page.settle();

    assert.equal(page.id("history-empty").hidden, false);

    page.close();
});

test("opening one from the history resumes it", async () => {
    const page = await bootConsole({ "chat/sessions": SESSIONS });
    await page.click(page.id("history"));

    await page.click(page.document.querySelectorAll(".session")[1]);

    const resumed = page.requests.find((request) => request.path === "chat/resume");
    assert.equal(JSON.parse(resumed.body).session, SESSIONS.sessions[1].id);
    assert.equal(page.id("history-overlay").hidden, true);

    page.close();
});

test("a conversation can be renamed from the history sheet", async () => {
    const page = await bootConsole({ "chat/sessions": SESSIONS });
    await page.click(page.id("history"));
    // Both row actions are icon buttons now, told apart by what they say they do.
    const rename = [...page.document.querySelectorAll(".session__act")]
        .find((button) => /Rename/.test(button.title));

    await page.click(rename);
    const field = page.$(".session__renameform input");
    assert.equal(field.value, "The boiler");
    field.value = "A better name";
    await page.submit(page.$(".session__renameform"));

    const renamed = page.requests.find((request) => request.path === "chat/rename");
    assert.deepEqual(JSON.parse(renamed.body), {
        session: SESSIONS.sessions[0].id,
        title: "A better name",
    });

    page.close();
});

test("the name in the header can be edited in place", async () => {
    const page = await bootConsole({
        chat: {
            ...CHAT,
            session: "abcdef12-0000-4000-8000-000000000001",
            title: "Untitled",
            turns: [{ role: "user", text: "hello", at: "2026-08-12T10:00:00Z" }],
        },
    });

    await page.click(page.id("chat-title"));

    const field = page.$(".title__form input");
    assert.equal(field.value, "Untitled");
    field.value = "Named by hand";
    await page.submit(page.$(".title__form"));

    const renamed = page.requests.find((request) => request.path === "chat/rename");
    assert.equal(JSON.parse(renamed.body).title, "Named by hand");

    page.close();
});

test("the header name cannot be edited before there is a conversation", async () => {
    const page = await bootConsole();

    assert.equal(page.id("chat-title").disabled, true);
    assert.equal(page.text("#chat-title"), "New conversation");

    page.close();
});

test("compact is only ever sent once the button is there to press", async () => {
    const page = await bootConsole({
        chat: {
            ...CHAT,
            session: "abcdef12-0000-4000-8000-000000000001",
            context: { used: 800, window: 1000, left_percent: 20 },
        },
    });

    await page.click(page.id("compact"));

    assert.notEqual(
        page.requests.find((request) => request.path === "chat/compact"),
        undefined,
    );

    page.close();
});

test("a skill is uploaded only after it has been chosen", async () => {
    const page = await bootConsole();
    await page.click(page.id("settings-open"));

    assert.equal(page.id("upload").disabled, true);

    const archive = new page.window.File([new Uint8Array([31, 139, 8, 0])], "release-notes.tar.gz", {
        type: "application/gzip",
    });
    await page.choose(page.id("file"), archive);

    assert.equal(page.id("upload").disabled, false);
    assert.match(page.text("#drop-text"), /release-notes\.tar\.gz/);

    await page.click(page.id("upload"));

    const uploaded = page.requests.find((request) => request.method === "POST");
    assert.match(uploaded.path, /^skills/);

    page.close();
});

test("something that is not a gzip is refused before it is sent", async () => {
    const page = await bootConsole();
    await page.click(page.id("settings-open"));

    const wrong = new page.window.File([new Uint8Array([1, 2, 3])], "notes.txt", {
        type: "text/plain",
    });
    await page.choose(page.id("file"), wrong);

    assert.equal(page.id("upload").disabled, true);
    assert.match(page.text("#error-banner"), /\.tar\.gz/);
    assert.equal(
        page.requests.some((request) => request.method === "POST"),
        false,
    );

    page.close();
});

test("the permissions file is saved as it stands", async () => {
    const page = await bootConsole();
    await page.click(page.id("settings-open"));
    await page.click(page.id("tab-permissions"));

    page.id("settings-json").value = '{"permissions": {"allow": ["Bash(git *)"]}}';
    page.id("settings-json").dispatchEvent(new page.window.Event("input", { bubbles: true }));
    await page.settle();
    assert.equal(page.id("settings-save").disabled, false);

    await page.click(page.id("settings-save"));

    const saved = page.requests.find((request) => request.method === "PUT");
    assert.equal(saved.path, "settings");
    assert.deepEqual(JSON.parse(saved.body), { permissions: { allow: ["Bash(git *)"] } });

    page.close();
});

test("format tidies the permissions file without sending anything", async () => {
    const page = await bootConsole();
    await page.click(page.id("settings-open"));
    await page.click(page.id("tab-permissions"));
    page.id("settings-json").value = '{"env":{"A":"1"}}';
    page.id("settings-json").dispatchEvent(new page.window.Event("input", { bubbles: true }));
    await page.settle();

    await page.click(page.id("settings-format"));

    assert.match(page.id("settings-json").value, /\n {2}"env"/);
    assert.equal(
        page.requests.some((request) => request.method === "PUT"),
        false,
    );

    page.close();
});

test("starting a fresh conversation asks the add-on to forget the current one", async () => {
    const page = await bootConsole({
        chat: { ...CHAT, session: "abcdef12-0000-4000-8000-000000000001" },
    });

    await page.click(page.id("new-chat"));

    assert.notEqual(
        page.requests.find((request) => request.path === "chat/new"),
        undefined,
    );

    page.close();
});

test("a message queues instead of being refused while Claude is answering", async () => {
    const page = await bootConsole({
        chat: {
            ...CHAT,
            session: "abcdef12-0000-4000-8000-000000000001",
            pending: [
                {
                    id: "job1",
                    status: "running",
                    prompt: "first",
                    partial: "wor",
                    created_at: "2026-08-12T10:00:00Z",
                },
            ],
        },
    });
    assert.equal(page.text("#send"), "Queue");

    page.id("prompt").value = "and this one too";
    await page.press(page.id("prompt"), "Enter");

    const queued = page.requests.find(
        (request) => request.method === "POST" && request.path === "chat",
    );
    assert.equal(JSON.parse(queued.body).prompt, "and this one too");

    page.close();
});

test("the model, effort and mode chosen are what the message carries", async () => {
    const page = await bootConsole();
    page.id("model").value = "haiku";
    page.id("effort").value = "high";
    page.id("mode").value = "plan";
    page.id("prompt").value = "with my own settings";

    await page.press(page.id("prompt"), "Enter");

    const sent = page.requests.find(
        (request) => request.method === "POST" && request.path === "chat",
    );
    assert.deepEqual(JSON.parse(sent.body), {
        prompt: "with my own settings",
        model: "haiku",
        effort: "high",
        permission_mode: "plan",
    });

    page.close();
});

test("one failed poll of the conversation is tolerated silently", async () => {
    // The transcript is polled once a second; a single miss is not news, and saying
    // so on every one of them made the banner flicker.
    const page = await bootConsole({ chat: () => new Error("connection reset") });

    assert.equal(page.id("error-banner").hidden, true);

    page.close();
});

test("the update banner can be dismissed and stays dismissed", async () => {
    const page = await bootConsole({
        health: {
            ...HEALTH,
            update: {
                status: "done",
                changed: true,
                previous: "2.1.227",
                installed: "2.1.228",
                finished_at: "2026-08-12T09:00:00+00:00",
            },
        },
    });
    assert.equal(page.id("update-banner").hidden, false);

    await page.click(page.id("update-dismiss"));

    assert.equal(page.id("update-banner").hidden, true);
    assert.equal(
        page.window.localStorage.getItem("dismissedUpdate"),
        "2026-08-12T09:00:00+00:00",
    );

    page.close();
});
