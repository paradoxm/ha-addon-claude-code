// Boots the add-on's real page — index.html, style.css and app.js, unmodified —
// inside jsdom, with the HTTP API replaced by a table of answers.
//
// Why this and not React with Jest: the UI is deliberately three static files that
// the image copies verbatim, with no build step, so a component framework and its
// toolchain would be the largest thing in the add-on. What the standard actually
// asks for — behaviour tested through the DOM the user sees, with spies where a
// call has to be observed — is what this gives, using Node's own test runner.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { JSDOM } from "jsdom";

const WWW = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "claude-code", "www");

export const HEALTH = {
    status: "ok",
    claude_version: "2.1.228",
    available_version: "2.1.228",
    update_available: false,
    update_channel: "latest",
    auto_update: true,
    updating: false,
    update: { status: "idle" },
    logged_in: true,
    skills: 0,
    queued: 0,
    job_running: false,
    default_model: "opus",
    default_effort: "medium",
    default_permission_mode: "manual",
    models: ["opus", "sonnet", "haiku", "fable"],
    efforts: ["low", "medium", "high", "xhigh", "max"],
    permission_modes: ["manual", "plan", "acceptEdits", "auto", "dontAsk"],
    timeout_minutes: 90,
    token_required: false,
    timezone: "Asia/Yekaterinburg",
};

export const CHAT = {
    session: null,
    title: null,
    turns: [],
    pending: [],
    failed: null,
    notices: [],
    context: null,
};

export const USAGE = {
    available: true,
    session: { percent: 34.0, threshold: 90, resets_at: new Date(Date.now() + 136 * 60_000).toISOString() },
    week: { percent: 8.0, threshold: 90, resets_at: new Date(Date.now() + 3 * 86_400_000).toISOString() },
    worst: { kind: "session", percent: 34.0, threshold: 90, resets_at: new Date(Date.now() + 136 * 60_000).toISOString() },
    thresholds: { session: 90, week: 90 },
    enough: true,
    checked_at: new Date().toISOString(),
};

const DEFAULT_ROUTES = {
    "health": () => HEALTH,
    "skills": () => ({ skills: [] }),
    "chat": () => CHAT,
    "chat/sessions": () => ({ sessions: [], current: null }),
    "settings": () => ({ settings: {}, path: "/data/home/.claude/settings.json", enforced_env: {} }),
    // The write routes answer plainly: a test that drives one of these cares about
    // the request, not about the reply.
    "chat/new": () => ({ session: null }),
    "chat/resume": () => ({ session: "abcdef12-0000-4000-8000-000000000001", turns: [] }),
    "chat/rename": () => ({ id: "job-rename", status: "queued", command: "rename" }),
    "chat/compact": () => ({ id: "job-compact", status: "queued", command: "compact" }),
    "usage": () => USAGE,
    "update": () => ({ status: "running", target: "latest" }),
    "version": () => ({
        installed: "2.1.228",
        available: "2.1.228",
        channel: "latest",
        update_available: false,
        auto_update: true,
        binary: "/data/home/.local/bin/claude",
        last_update: { status: "idle" },
    }),
};

/**
 * Close the page when done — a jsdom window holds timers, and an open one keeps the
 * runner's process alive. That is why the npm script runs with --test-force-exit: a test
 * that throws before its close() would otherwise turn a plain failure into a silent hang,
 * which is exactly how one afternoon went.
 *
 * @param {object} answers  path (without the `api/` prefix) -> payload or function
 * @returns the window, the request log, and helpers to advance the page
 */
export async function bootConsole(answers = {}) {
    const routes = { ...DEFAULT_ROUTES, ...answers };
    const requests = [];

    const dom = new JSDOM(readFileSync(join(WWW, "index.html"), "utf8"), {
        runScripts: "outside-only",
        url: "http://localhost/api/hassio_ingress/token/",
    });
    const { window } = dom;

    window.fetch = (path, options = {}) => {
        const key = String(path).replace(/^api\//, "");
        requests.push({ path: key, method: options.method || "GET", body: options.body });
        const answer = routes[key.split("?")[0]] ?? routes[key];
        if (answer === undefined) {
            return Promise.resolve(jsonResponse(404, { error: `no stub for ${key}` }));
        }
        const payload = typeof answer === "function" ? answer(requests) : answer;
        if (payload instanceof Error) {
            return Promise.resolve(jsonResponse(500, { error: payload.message }));
        }
        return Promise.resolve(jsonResponse(200, payload));
    };

    function jsonResponse(status, payload) {
        return {
            ok: status < 400,
            status,
            headers: { get: () => "application/json" },
            json: () => Promise.resolve(payload),
            text: () => Promise.resolve(JSON.stringify(payload)),
        };
    }

    window.eval(readFileSync(join(WWW, "app.js"), "utf8"));

    // The page fetches health, skills and the conversation as it starts; letting
    // those promises resolve is what "the page has loaded" means here.
    const settle = async (times = 6) => {
        for (let index = 0; index < times; index += 1) {
            await new Promise((resolve) => window.setTimeout(resolve, 0));
        }
    };
    await settle();

    return {
        window,
        document: window.document,
        requests,
        routes,
        settle,
        $: (selector) => window.document.querySelector(selector),
        $$: (selector) => [...window.document.querySelectorAll(selector)],
        id: (value) => window.document.getElementById(value),
        text: (selector) => (window.document.querySelector(selector)?.textContent || "").trim(),
        click: async (element) => {
            element.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
            await settle();
        },
        // Waits real time, so a poll actually happens: the page refreshes the
        // conversation once a second and some behaviour only shows on a redraw.
        tick: async (ms = 1300) => {
            await new Promise((resolve) => window.setTimeout(resolve, ms));
            await settle();
        },
        submit: async (form) => {
            form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
            await settle();
        },
        choose: async (input, file) => {
            // jsdom will not let a FileList be assigned, so the property is replaced
            // for this one element — the same thing a real picker leaves behind.
            Object.defineProperty(input, "files", { value: [file], configurable: true });
            input.dispatchEvent(new window.Event("change", { bubbles: true }));
            await settle();
        },
        press: async (element, key, extra = {}) => {
            element.dispatchEvent(
                new window.KeyboardEvent("keydown", { key, bubbles: true, ...extra }),
            );
            await settle();
        },
        close: () => {
            window.close();
        },
    };
}
