import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

import { bootConsole, CHAT, HEALTH } from "./console.mjs";

test("shows what the add-on reports about itself", async () => {
    const page = await bootConsole();

    assert.equal(page.text("#r-version"), "2.1.228");
    assert.equal(page.text("#r-login"), "signed in");
    assert.equal(page.text("#r-updates"), "auto · latest");
    assert.equal(page.id("login-banner").hidden, true);

    page.close();
});

test("counts what is queued, the running turn included", async () => {
    const page = await bootConsole({ health: { ...HEALTH, queued: 2, job_running: true } });

    assert.equal(page.text("#r-queue"), "3");
    assert.equal(page.text("#r-skills"), "0");

    page.close();
});

test("says so when nobody has signed in yet", async () => {
    const page = await bootConsole({ health: { ...HEALTH, logged_in: false } });

    assert.equal(page.id("login-banner").hidden, false);
    assert.equal(page.text("#r-login"), "not signed in");

    page.close();
});

test("offers the models, efforts and modes the server accepts, and nothing else", async () => {
    const page = await bootConsole();

    const values = (id) => [...page.id(id).options].map((option) => option.value);
    assert.deepEqual(values("model"), HEALTH.models);
    assert.deepEqual(values("effort"), HEALTH.efforts);
    assert.deepEqual(values("mode"), HEALTH.permission_modes);
    assert.equal(page.id("model").value, "opus");
    assert.equal(page.id("mode").value, "manual");

    page.close();
});

test("shows both sides of the conversation", async () => {
    const page = await bootConsole({
        chat: {
            ...CHAT,
            session: "abcdef12-0000-4000-8000-000000000000",
            title: "About the boiler",
            turns: [
                { role: "user", text: "look at the boiler log", at: "2026-08-12T10:00:00Z" },
                { role: "assistant", text: "Here it is.", at: "2026-08-12T10:00:09Z" },
            ],
        },
    });

    const turns = [...page.document.querySelectorAll(".turn")];
    assert.equal(turns.length, 2);
    assert.match(turns[0].className, /turn--user/);
    assert.equal(turns[0].querySelector(".turn__body").textContent, "look at the boiler log");
    assert.equal(turns[1].querySelector(".turn__body").textContent, "Here it is.");
    assert.equal(page.text("#chat-title"), "About the boiler");
    assert.equal(page.id("transcript-empty").hidden, true);

    page.close();
});

test("sends the message on Enter", async () => {
    const page = await bootConsole({ chat: () => CHAT });
    const prompt = page.id("prompt");
    prompt.value = "look at the boiler log";

    await page.press(prompt, "Enter");

    const sent = page.requests.find((request) => request.method === "POST");
    assert.equal(sent.path, "chat");
    assert.deepEqual(JSON.parse(sent.body).prompt, "look at the boiler log");
    assert.equal(prompt.value, "");

    page.close();
});

test("Shift+Enter does not send", async () => {
    const page = await bootConsole();
    const prompt = page.id("prompt");
    prompt.value = "first line";

    await page.press(prompt, "Enter", { shiftKey: true });

    assert.equal(page.requests.some((request) => request.method === "POST"), false);
    assert.equal(prompt.value, "first line");

    page.close();
});

test("an empty message is not sent", async () => {
    const page = await bootConsole();
    page.id("prompt").value = "   ";

    await page.press(page.id("prompt"), "Enter");

    assert.equal(page.requests.some((request) => request.method === "POST"), false);

    page.close();
});

test("shows the reply as it arrives, with a way to stop it", async () => {
    const page = await bootConsole({
        chat: {
            ...CHAT,
            session: "abcdef12-0000-4000-8000-000000000000",
            turns: [{ role: "user", text: "a long one", at: "2026-08-12T10:00:00Z" }],
            pending: [
                {
                    id: "job1",
                    status: "running",
                    prompt: "a long one",
                    partial: "half a rep",
                    created_at: "2026-08-12T10:00:00Z",
                },
            ],
        },
    });

    assert.match(page.text(".turn--working .turn__body"), /half a rep/);
    assert.equal(page.$(".turn--working .caret") !== null, true);
    assert.equal(page.text("#send"), "Queue");

    const users = [...page.document.querySelectorAll(".turn--user")];
    assert.equal(users.length, 1, "the message the transcript already holds is not drawn twice");

    await page.click(page.$(".turn--working button"));
    const cancelled = page.requests.find((request) => request.path.endsWith("/cancel"));
    assert.equal(cancelled.path, "jobs/job1/cancel");

    page.close();
});

function withGeometry(page, { scrollHeight = 2000, clientHeight = 400, scrollTop = 0 } = {}) {
    // jsdom does no layout, so the three numbers the decision is made from are stated
    // here: a long transcript, a short window, and where the reader is in it.
    const view = page.id("transcript");
    Object.defineProperty(view, "scrollHeight", { value: scrollHeight, configurable: true });
    Object.defineProperty(view, "clientHeight", { value: clientHeight, configurable: true });
    view.scrollTop = scrollTop;
    return view;
}

test("scrolling up to re-read something is not undone by the next word", async () => {
    const streaming = {
        ...CHAT,
        session: "abcdef12-0000-4000-8000-000000000000",
        turns: [{ role: "user", text: "a long one", at: "2026-08-12T10:00:00Z" }],
        pending: [
            {
                id: "job1",
                status: "running",
                prompt: "a long one",
                partial: "the first half",
                created_at: "2026-08-12T10:00:00Z",
            },
        ],
    };
    const page = await bootConsole({ chat: () => streaming });

    const view = withGeometry(page, { scrollTop: 300 });
    view.dispatchEvent(new page.window.Event("scroll"));
    await page.settle();

    assert.equal(page.id("to-latest").hidden, false, "the way back is offered");

    streaming.pending[0].partial = "the first half and more";
    await page.tick();

    assert.match(page.text(".turn--working .turn__body"), /and more/, "the poll happened");
    assert.equal(view.scrollTop, 300, "the view stayed where it was put");

    page.close();
});

test("a reader at the end is carried along by the text", async () => {
    const streaming = {
        ...CHAT,
        session: "abcdef12-0000-4000-8000-000000000000",
        pending: [
            {
                id: "job1",
                status: "running",
                prompt: "a long one",
                partial: "the first half",
                created_at: "2026-08-12T10:00:00Z",
            },
        ],
    };
    const page = await bootConsole({ chat: () => streaming });

    const view = withGeometry(page, { scrollTop: 1600 });
    view.dispatchEvent(new page.window.Event("scroll"));

    streaming.pending[0].partial = "the first half and more";
    await page.tick();

    assert.match(page.text(".turn--working .turn__body"), /and more/, "the poll happened");
    assert.equal(page.id("to-latest").hidden, true);
    assert.equal(view.scrollTop, view.scrollHeight);

    page.close();
});

test("the way back to the newest takes you there and stops offering itself", async () => {
    const page = await bootConsole({
        chat: {
            ...CHAT,
            session: "abcdef12-0000-4000-8000-000000000000",
            turns: [{ role: "user", text: "something to re-read", at: "2026-08-12T10:00:00Z" }],
        },
    });
    const view = withGeometry(page, { scrollTop: 120 });
    view.dispatchEvent(new page.window.Event("scroll"));
    await page.settle();
    assert.equal(page.id("to-latest").hidden, false);

    await page.click(page.id("to-latest"));

    assert.equal(view.scrollTop, view.scrollHeight);
    assert.equal(page.id("to-latest").hidden, true);

    page.close();
});

test("sending a message goes to the end whatever was being read", async () => {
    const page = await bootConsole({
        chat: {
            ...CHAT,
            session: "abcdef12-0000-4000-8000-000000000000",
            turns: [{ role: "user", text: "older", at: "2026-08-12T10:00:00Z" }],
        },
    });
    const view = withGeometry(page, { scrollTop: 50 });
    view.dispatchEvent(new page.window.Event("scroll"));
    await page.settle();

    page.id("prompt").value = "a new one";
    await page.press(page.id("prompt"), "Enter");

    assert.equal(view.scrollTop, view.scrollHeight);
    assert.equal(page.id("to-latest").hidden, true);

    page.close();
});

test("lists what is waiting behind the running turn", async () => {
    const page = await bootConsole({
        chat: {
            ...CHAT,
            session: "abcdef12-0000-4000-8000-000000000000",
            pending: [
                { id: "job1", status: "running", prompt: "first", created_at: "2026-08-12T10:00:00Z" },
                { id: "job2", status: "queued", prompt: "second", created_at: "2026-08-12T10:00:01Z" },
                { id: "job3", status: "queued", prompt: "third", created_at: "2026-08-12T10:00:02Z" },
            ],
        },
    });

    const queued = [...page.document.querySelectorAll(".queued")];
    assert.equal(queued.length, 2);
    assert.match(page.text("#chat-queue"), /2 waiting/);
    assert.match(queued[0].textContent, /second/);

    page.close();
});

test("shows a failed turn with the reason, in the conversation", async () => {
    const page = await bootConsole({
        chat: {
            ...CHAT,
            session: "abcdef12-0000-4000-8000-000000000000",
            failed: {
                id: "job9",
                status: "failed",
                prompt: "the one that failed",
                error: "Not logged in · Please run /login",
                created_at: "2026-08-12T10:00:00Z",
                finished_at: "2026-08-12T10:00:02Z",
            },
        },
    });

    const failed = page.$(".turn--failed");
    assert.match(failed.textContent, /Not logged in/);

    page.close();
});

test("keeps warnings out of the conversation and behind a count", async () => {
    const page = await bootConsole({
        chat: {
            ...CHAT,
            session: "abcdef12-0000-4000-8000-000000000000",
            turns: [{ role: "user", text: "go on", at: "2026-08-12T10:00:00Z" }],
            notices: [
                {
                    kind: "informational",
                    level: "warning",
                    text: "Unknown command: /rname",
                    at: "2026-08-12T10:00:01Z",
                },
                {
                    kind: "tool_error",
                    level: "error",
                    text: "File has not been read yet.",
                    at: "2026-08-12T10:00:02Z",
                },
            ],
        },
    });

    assert.equal(page.id("notices").hidden, false);
    assert.equal(page.text("#notices-count"), "2 new notices");
    assert.equal(page.document.querySelectorAll(".turn").length, 1);
    assert.equal(page.id("notices-overlay").hidden, true);

    await page.click(page.id("notices"));

    const rows = [...page.document.querySelectorAll(".notice-row")];
    assert.equal(page.id("notices-overlay").hidden, false);
    assert.equal(rows.length, 2);
    assert.match(rows[0].textContent, /tool error/, "newest first");
    assert.match(rows[1].textContent, /Unknown command/);

    await page.press(page.document.body, "Escape");
    assert.equal(page.id("notices-overlay").hidden, true);

    page.close();
});

test("no warnings means no button at all", async () => {
    const page = await bootConsole();

    assert.equal(page.id("notices").hidden, true);

    page.close();
});

test("reading the warnings puts the button away and marks which were new", async () => {
    const chat = {
        ...CHAT,
        session: "abcdef12-0000-4000-8000-000000000000",
        notices: [
            {
                kind: "tool_error",
                level: "error",
                text: "File has not been read yet.",
                at: "2026-08-12T10:00:02Z",
            },
        ],
    };
    const page = await bootConsole({ chat: () => chat });
    assert.equal(page.id("notices").hidden, false);

    await page.click(page.id("notices"));

    const rows = [...page.document.querySelectorAll(".notice-row")];
    assert.equal(rows.length, 1);
    assert.match(rows[0].className, /notice-row--new/, "it was new when the list opened");

    await page.press(page.document.body, "Escape");

    assert.equal(page.id("notices").hidden, true, "nothing unread, nothing to show");

    page.close();
});

test("a warning that arrives after the others is the only one marked new", async () => {
    const chat = {
        ...CHAT,
        session: "abcdef12-0000-4000-8000-000000000000",
        notices: [
            { kind: "tool_error", level: "error", text: "the first", at: "2026-08-12T10:00:02Z" },
        ],
    };
    const page = await bootConsole({ chat: () => chat });
    await page.click(page.id("notices"));
    await page.press(page.document.body, "Escape");
    assert.equal(page.id("notices").hidden, true);

    chat.notices = [
        ...chat.notices,
        { kind: "api_error", level: "error", text: "the second", at: "2026-08-12T10:05:00Z" },
    ];
    // Idle, so nothing is polling: a redraw is what a sent message would cause.
    page.id("prompt").value = "anything";
    await page.press(page.id("prompt"), "Enter");
    await page.tick();

    assert.equal(page.id("notices").hidden, false);
    assert.equal(page.text("#notices-count"), "1 new notice");

    await page.click(page.id("notices"));

    const marked = [...page.document.querySelectorAll(".notice-row")].map((row) =>
        row.className.includes("notice-row--new"),
    );
    assert.deepEqual(marked, [true, false], "newest first, and only it is new");

    page.close();
});

test("the settings chip says what the next message will be sent with", async () => {
    const page = await bootConsole();

    assert.equal(page.text("#controls-summary"), "opus · medium · manual");
    assert.equal(page.id("controls-chip").getAttribute("aria-expanded"), "false");
    assert.equal(page.id("controls").className.includes("is-open"), false);

    await page.click(page.id("controls-chip"));

    assert.equal(page.id("controls-chip").getAttribute("aria-expanded"), "true");
    assert.equal(page.id("controls").className.includes("is-open"), true);

    page.id("model").value = "haiku";
    page.id("model").dispatchEvent(new page.window.Event("change", { bubbles: true }));
    await page.settle();

    assert.equal(page.text("#controls-summary"), "haiku · medium · manual");

    page.close();
});

test("the token count is only worth a line once the window is half used", async () => {
    const roomy = await bootConsole({
        chat: { ...CHAT, context: { used: 100, window: 1000, left_percent: 90 } },
    });
    assert.equal(roomy.id("context").dataset.short, "90% free");
    assert.match(roomy.id("context").className, /context--roomy/);
    roomy.close();

    const tight = await bootConsole({
        chat: { ...CHAT, context: { used: 900, window: 1000, left_percent: 10 } },
    });
    assert.equal(tight.id("context").dataset.short, "10% free");
    assert.equal(tight.id("context").className.includes("context--roomy"), false);
    assert.match(tight.id("context").className, /context--tight/);
    tight.close();
});

test("offers compact only once the window is more than half used", async () => {
    const roomy = await bootConsole({
        chat: { ...CHAT, context: { used: 100, window: 1000, left_percent: 90 } },
    });
    assert.equal(roomy.id("compact").hidden, true);
    assert.match(roomy.text("#context"), /90% free/);
    roomy.close();

    const tight = await bootConsole({
        chat: { ...CHAT, context: { used: 700, window: 1000, left_percent: 30 } },
    });
    assert.equal(tight.id("compact").hidden, false);
    tight.close();
});

test("the context bar appears for a warning even before any context is reported", async () => {
    const page = await bootConsole({
        chat: {
            ...CHAT,
            notices: [{ kind: "api_error", level: "error", text: "API Error", at: null }],
        },
    });

    assert.equal(page.id("contextbar").hidden, false);
    assert.equal(page.id("context").hidden, true);

    page.close();
});

test("reports the API being unreachable instead of failing silently", async () => {
    const page = await bootConsole({ health: new Error("connection refused") });

    assert.equal(page.id("error-banner").hidden, false);
    assert.match(page.text("#error-banner"), /connection refused/);

    page.close();
});

test("a turn the add-on has frozen says paused, and offers to let it go", async () => {
    const page = await bootConsole({
        chat: () => ({
            ...CHAT,
            session: "abcdef12-0000-4000-8000-000000000001",
            turns: [{ role: "user", text: "write the texts", at: null }],
            pending: [{
                id: "job-frozen", status: "running", prompt: "write the texts",
                partial: "Пишу вариант 2.", paused: true,
            }],
        }),
    });

    const pill = page.$(".turn--working .pill");
    assert.equal(pill.textContent, "paused");
    assert.ok(pill.classList.contains("is-paused"), "steady, not the working pulse");

    const letGo = [...page.$(".turn--working").querySelectorAll("button")]
        .find((button) => button.textContent === "let it go");
    assert.ok(letGo, "a frozen turn needs an exit that is not cancel");

    await page.click(letGo);

    assert.ok(page.requests.some(({ path, method }) =>
        path === "jobs/job-frozen/resume" && method === "POST"));
    page.close();
});

test("and a working turn still says working, with no such button", async () => {
    const page = await bootConsole({
        chat: () => ({
            ...CHAT,
            session: "abcdef12-0000-4000-8000-000000000001",
            turns: [{ role: "user", text: "write the texts", at: null }],
            pending: [{ id: "job-live", status: "running", prompt: "write the texts", partial: "Пишу." }],
        }),
    });

    assert.equal(page.$(".turn--working .pill").textContent, "working");
    assert.ok(![...page.$(".turn--working").querySelectorAll("button")]
        .some((button) => button.textContent === "let it go"));
    page.close();
});

test("a console that will answer nothing says why, in red, without being asked", async () => {
    const page = await bootConsole({
        usage: () => ({
            available: true, acting: true, enough: false, thresholds: { session: 90, week: 90 },
            worst: { kind: "session", percent: 96, threshold: 90,
                     resets_at: new Date(Date.now() + 90 * 60_000).toISOString() },
            checked_at: new Date().toISOString(),
        }),
    });

    const banner = page.id("limits-banner");
    assert.equal(banner.hidden, false);
    assert.match(banner.textContent, /Work is held/);
    assert.match(banner.textContent, /five-hour allowance is 96% used/);
    assert.match(banner.textContent, /stops at 90%/);
    assert.match(banner.textContent, /resets in 1 hr 30 min/);
    page.close();
});

test("and says nothing when the add-on only reports the figure", async () => {
    const page = await bootConsole({
        usage: () => ({
            available: true, acting: false, enough: false, thresholds: { session: 90, week: 90 },
            worst: { kind: "week", percent: 99, resets_at: new Date().toISOString() },
            checked_at: new Date().toISOString(),
        }),
    });

    assert.equal(page.id("limits-banner").hidden, true,
        "nothing is held, so saying so would be a lie");
    page.close();
});

test("nor when there is room, nor when the reading cannot be had", async () => {
    const withRoom = await bootConsole();
    assert.equal(withRoom.id("limits-banner").hidden, true);
    withRoom.close();

    const blind = await bootConsole({ usage: () => ({ available: false, reason: "not signed in" }) });
    assert.equal(blind.id("limits-banner").hidden, true);
    blind.close();
});

test("the allowance is asked for on a slow beat, not on every health tick", async () => {
    const page = await bootConsole();
    const asked = () => page.requests.filter(({ path }) => path === "usage").length;
    assert.equal(asked(), 1, "once as the page opens");

    await page.tick(1300);
    await page.tick(1300);

    assert.equal(asked(), 1, "and not again for three minutes");
    page.close();
});

test("a long waiting prompt cannot push the composer off the page", async () => {
    // A skill's prompt runs to dozens of lines. Rendered whole, it took the page's
    // scrolling with it and left nothing clickable.
    const page = await bootConsole({
        chat: () => ({
            ...CHAT,
            pending: [{
                id: "job-waiting", status: "created",
                prompt: Array.from({ length: 40 }, (_, i) => `line ${i} of a very long prompt`).join("\n"),
            }],
        }),
    });

    assert.ok(page.$(".queued__text"), "the waiting message is shown");
    // jsdom does not lay anything out, so what can be checked is that the rules which
    // bound it are there: the clamp on the text and a ceiling on the block.
    const css = readFileSync(new URL("../../claude-code/www/style.css", import.meta.url), "utf8");
    const queued = css.slice(css.indexOf(".queued__text {"), css.indexOf("}", css.indexOf(".queued__text {")));
    const queue = css.slice(css.indexOf(".queue {"), css.indexOf("}", css.indexOf(".queue {")));
    assert.match(queued, /line-clamp: 3/);
    assert.match(queued, /overflow: hidden/);
    assert.match(queue, /max-height/);
    assert.match(queue, /overflow-y: auto/);
    page.close();
});

test("and it can be removed, even though it never started", async () => {
    const page = await bootConsole({
        chat: () => ({
            ...CHAT,
            pending: [{ id: "job-waiting", status: "created", prompt: "a message nobody wants" }],
        }),
    });

    const drop = [...page.$$(".queued button")].find((b) => b.textContent === "remove");
    await page.click(drop);

    assert.ok(page.requests.some(({ path, method }) =>
        path === "jobs/job-waiting" && method === "DELETE"));
    page.close();
});

const SESSIONS = {
    sessions: [
        { id: "abcdef12-0000-4000-8000-000000000001", title: "Heating schedule", custom: false,
          preview: "write the texts", messages: 12, updated_at: "2026-08-13T09:00:00+00:00" },
    ],
    current: null,
};

async function openHistory(answers = {}) {
    const page = await bootConsole({ "chat/sessions": () => SESSIONS, ...answers });
    await page.click(page.id("history"));
    return page;
}

test("deleting a conversation takes two presses, and says which press it is on", async () => {
    const page = await openHistory();
    const drop = page.$$(".session__act.icon--danger")[0];
    assert.ok(drop, "every conversation offers it");
    assert.match(drop.title, /Delete/);
    assert.ok(drop.querySelector("svg"), "an icon, not a wall of uppercase");

    await page.click(drop);

    assert.match(drop.title, /Press again/);
    assert.ok(drop.classList.contains("is-armed"));
    assert.ok(!page.requests.some(({ method }) => method === "DELETE"), "nothing yet");

    await page.click(drop);

    assert.ok(page.requests.some(({ path, method }) =>
        path === "chat/sessions/abcdef12-0000-4000-8000-000000000001" && method === "DELETE"));
    page.close();
});

test("a delete that the add-on refuses says so and disarms", async () => {
    const page = await openHistory({
        "chat/sessions/abcdef12-0000-4000-8000-000000000001": () =>
            new Error("a turn of this conversation is still running"),
    });
    const drop = page.$$(".session__act.icon--danger")[0];

    await page.click(drop);
    await page.click(drop);

    assert.match(page.id("error-banner").textContent, /still running/);
    assert.match(drop.title, /^Delete/, "ready to be asked again, not stuck mid-question");
    assert.ok(!drop.classList.contains("is-armed"));
    assert.equal(drop.disabled, false);
    page.close();
});

test("shows the time of a message on Home Assistant's clock", async () => {
    // The add-on stamps everything in UTC. The offset used to be deleted along with the
    // microseconds, so every message in the console read five hours early here — with
    // nothing on the page to say it was another country's time.
    const page = await bootConsole({
        chat: {
            ...CHAT,
            session: "abcdef12-0000-4000-8000-000000000000",
            turns: [{ role: "assistant", text: "Waiting on the reviews.",
                      at: "2026-08-14T07:34:53.124379+00:00" }],
        },
    });

    assert.equal(page.document.querySelector(".turn__when").textContent, "2026-08-14 12:34:53");

    page.close();
});
