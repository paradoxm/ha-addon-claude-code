import assert from "node:assert/strict";
import test from "node:test";

import { bootConsole, USAGE } from "./console.mjs";

async function openUsage(answers = {}) {
    const page = await bootConsole(answers);
    await page.click(page.id("settings-open"));
    await page.click(page.id("tab-usage"));
    return page;
}

test("both windows are shown with what is used and when they come back", async () => {
    const page = await openUsage();

    assert.match(page.id("usage-session-figure").textContent, /34% used/);
    assert.match(page.id("usage-session-when").textContent, /Resets in 2 hr 16 min/);
    assert.equal(page.id("usage-session-bar").style.width, "34%");
    assert.match(page.id("usage-week-figure").textContent, /8% used/);
    assert.match(page.id("usage-week-when").textContent, /Resets in 3 days/);
    assert.match(page.id("usage-rule").textContent, /stops at 90%/);
    page.close();
});

test("nothing is said about limits when there is room", async () => {
    const page = await openUsage();

    assert.equal(page.id("usage-note").textContent, "");
    page.close();
});

test("the colour follows how full the window is, not what the add-on stops at", async () => {
    // A low threshold used to paint a half-empty window red, which said nothing about
    // how much room was left.
    const page = await openUsage({
        usage: () => ({
            ...USAGE,
            session: { percent: 46, threshold: 20, resets_at: USAGE.session.resets_at },
            week: { percent: 9, threshold: 20, resets_at: USAGE.week.resets_at },
            thresholds: { session: 20, week: 20 },
            enough: false,
        }),
    });

    const week = page.id("usage-week-bar").classList;
    assert.ok(!week.contains("meter__fill--close") && !week.contains("meter__fill--spent"),
        "nine per cent is green: it is not the window holding anything up");
    assert.match(page.id("usage-note").textContent, /refused/);
    assert.ok(page.id("usage-note").classList.contains("hint--bad"));
    page.close();
});

test("a window that has reached the figure the add-on stops at goes red and says why", async () => {
    const page = await openUsage({
        usage: () => ({
            ...USAGE,
            session: { percent: 46, threshold: 20, resets_at: USAGE.session.resets_at },
            thresholds: { session: 20, week: 20 },
            enough: false,
        }),
    });

    assert.ok(page.id("usage-session-bar").classList.contains("meter__fill--spent"),
        "the reason nothing runs is this window, whatever the figure is");
    assert.match(page.id("usage-session-when").textContent, /Work is held until this resets/);
    assert.ok(page.id("usage-session-when").classList.contains("hint--bad"));
    page.close();
});

test("amber past seventy, red past ninety", async () => {
    const page = await openUsage({
        usage: () => ({
            ...USAGE,
            session: { percent: 71, resets_at: USAGE.session.resets_at },
            week: { percent: 90, resets_at: USAGE.week.resets_at },
        }),
    });

    assert.ok(page.id("usage-session-bar").classList.contains("meter__fill--close"));
    assert.ok(page.id("usage-week-bar").classList.contains("meter__fill--spent"));
    page.close();
});

test("a reset time is shown on Home Assistant's clock, not the browser's", async () => {
    const page = await openUsage({
        usage: () => ({
            ...USAGE,
            session: { percent: 34, resets_at: "2026-08-13T13:19:00Z" },
        }),
    });

    // Yekaterinburg is UTC+5, so thirteen hundred in Greenwich is six in the evening here.
    assert.match(page.id("usage-session-when").textContent, /13 Aug, 18:19/);
    page.close();
});

test("the figure the guard stops at is marked on the bar", async () => {
    const page = await openUsage({ usage: () => ({ ...USAGE, thresholds: { session: 75, week: 90 } }) });

    const mark = page.id("usage-session-mark");
    assert.equal(mark.hidden, false);
    assert.equal(mark.style.left, "75%");
    assert.match(mark.title, /stops at 75%/);
    page.close();
});

test("and nothing is marked when the guard cannot stop anything", async () => {
    const page = await openUsage({ usage: () => ({ ...USAGE, thresholds: { session: 100, week: 100 } }) });

    assert.equal(page.id("usage-session-mark").hidden, true);
    page.close();
});

test("a reading that could not be had says so, and says work goes on anyway", async () => {
    const page = await openUsage({
        usage: () => ({ available: false, reason: "not signed in", checked_at: USAGE.checked_at }),
    });

    assert.match(page.id("usage-note").textContent, /could not be read: not signed in/);
    assert.match(page.id("usage-note").textContent, /never held back/);
    assert.equal(page.id("usage-session-when").textContent, "not reported");
    page.close();
});

test("refresh asks the add-on to read it again rather than serving the cache", async () => {
    const page = await openUsage();

    await page.click(page.id("usage-refresh"));

    assert.ok(page.requests.some(({ path }) => path === "usage?refresh"),
        `asked for: ${page.requests.map(({ path }) => path).join(", ")}`);
    page.close();
});

test("a refusal for asking too often does not send anybody to the terminal", async () => {
    const page = await openUsage({
        usage: () => ({
            available: false,
            reason: "asked too often; leaving it alone for a while",
            retry_at: new Date(Date.now() + 12 * 60_000).toISOString(),
            thresholds: { session: 90, week: 90 }, acting: true, check_every: 180,
            checked_at: new Date().toISOString(),
        }),
    });

    const note = page.id("usage-note").textContent;
    assert.match(note, /asked too often/);
    assert.match(note, /try again in 12 min/);
    assert.doesNotMatch(note, /Signing in/, "it is not a sign-in problem");
    assert.match(note, /never held back/);
    page.close();
});

test("each window is marked at its own figure", async () => {
    // The whole point of two settings: the week can be held to a stricter figure than the
    // five-hour window, and the page has to show where each one stands.
    const page = await openUsage({
        usage: () => ({
            ...USAGE,
            session: { percent: 34, threshold: 95, resets_at: USAGE.session.resets_at },
            week: { percent: 60, threshold: 65, resets_at: USAGE.week.resets_at },
            thresholds: { session: 95, week: 65 },
        }),
    });

    assert.equal(page.id("usage-session-mark").style.left, "95%");
    assert.equal(page.id("usage-week-mark").style.left, "65%");
    assert.match(page.id("usage-rule").textContent, /95% of the five-hour window or 65% of the week/);
    page.close();
});

test("a window over its own figure goes red while the other stays green", async () => {
    const page = await openUsage({
        usage: () => ({
            ...USAGE,
            session: { percent: 34, threshold: 95, resets_at: USAGE.session.resets_at },
            week: { percent: 70, threshold: 65, resets_at: USAGE.week.resets_at },
            thresholds: { session: 95, week: 65 },
            worst: { kind: "week", percent: 70, threshold: 65, resets_at: USAGE.week.resets_at },
            enough: false,
        }),
    });

    assert.ok(page.id("usage-week-bar").classList.contains("meter__fill--spent"));
    assert.ok(!page.id("usage-session-bar").classList.contains("meter__fill--spent"),
        "the session window is at a third of its own figure and holds nothing up");
    assert.match(page.id("usage-week-when").textContent, /Work is held until this resets/);
    page.close();
});
