import assert from "node:assert/strict";
import test from "node:test";

import { bootConsole, CHAT, HEALTH } from "./console.mjs";

const SKILLS = {
    skills: [
        {
            name: "release-notes",
            files: 12,
            bytes: 48_000,
            has_skill_md: true,
            description: "Writes release notes from a changelog",
            updated_at: "2026-08-01T09:00:00+00:00",
        },
        {
            name: "half-finished",
            files: 2,
            bytes: 300,
            has_skill_md: false,
            description: null,
            updated_at: "2026-08-02T09:00:00+00:00",
        },
    ],
};

async function openSettings(answers = {}, tab = "tab-skills") {
    const page = await bootConsole(answers);
    await page.click(page.id("settings-open"));
    await page.click(page.id(tab));
    return page;
}

test("the settings sheet opens on what is asked for most — the allowance", async () => {
    const page = await bootConsole();
    assert.equal(page.id("settings-overlay").hidden, true);

    await page.click(page.id("settings-open"));

    assert.equal(page.id("settings-overlay").hidden, false);
    assert.equal(page.id("pane-usage").hidden, false, "the allowance, not the skills");
    assert.equal(page.id("pane-skills").hidden, true);
    assert.equal(page.id("pane-permissions").hidden, true);
    assert.equal(page.id("pane-updates").hidden, true);

    await page.press(page.document.body, "Escape");
    assert.equal(page.id("settings-overlay").hidden, true);

    page.close();
});

test("lists each installed skill with what it is and what it weighs", async () => {
    const page = await openSettings({ health: { ...HEALTH, skills: 2 }, skills: SKILLS });

    const rows = [...page.document.querySelectorAll(".skill")];
    assert.equal(rows.length, 2);
    assert.match(rows[0].textContent, /release-notes/);
    assert.match(rows[0].textContent, /Writes release notes/);
    assert.match(rows[0].textContent, /12 files/);
    assert.match(rows[0].textContent, /47 kb/);
    assert.equal(page.id("skills-empty").hidden, true);

    page.close();
});

test("says plainly when a skill has no SKILL.md", async () => {
    const page = await openSettings({ health: { ...HEALTH, skills: 2 }, skills: SKILLS });

    const broken = [...page.document.querySelectorAll(".skill")][1];

    assert.match(broken.textContent, /no SKILL\.md/i);

    page.close();
});

test("nothing installed says so instead of showing an empty box", async () => {
    const page = await openSettings();

    assert.equal(page.id("skills-empty").hidden, false);
    assert.equal(page.document.querySelectorAll(".skill").length, 0);

    page.close();
});

test("deleting a skill takes two clicks", async () => {
    const page = await openSettings({ health: { ...HEALTH, skills: 2 }, skills: SKILLS });
    const remove = [...page.document.querySelectorAll(".skill button")].find(
        (button) => button.textContent.trim() === "delete",
    );

    await page.click(remove);

    assert.equal(
        page.requests.some((request) => request.method === "DELETE"),
        false,
        "the first click only arms the button",
    );

    await page.click(remove);

    const deleted = page.requests.find((request) => request.method === "DELETE");
    assert.equal(deleted.path, "skills/release-notes");

    page.close();
});

test("the permissions tab shows the file and refuses to save broken JSON", async () => {
    const page = await openSettings(
        {
            settings: {
                settings: { permissions: { allow: ["Bash(git *)"] } },
                path: "/data/home/.claude/settings.json",
                enforced_env: { USE_BUILTIN_RIPGREP: "0" },
            },
        },
        "tab-permissions",
    );

    assert.match(page.text("#settings-path"), /settings\.json/);
    assert.match(page.id("settings-json").value, /Bash\(git \*\)/);

    page.id("settings-json").value = "{ not json";
    page.id("settings-json").dispatchEvent(new page.window.Event("input", { bubbles: true }));
    await page.settle();

    assert.equal(page.id("settings-save").disabled, true);
    assert.match(page.text("#settings-status"), /JSON/);

    page.close();
});

test("the updates tab reports the versions and where the binary came from", async () => {
    const page = await openSettings({}, "tab-updates");

    assert.equal(page.text("#u-installed"), "2.1.228");
    assert.equal(page.text("#u-available"), "2.1.228");
    assert.equal(page.text("#u-channel"), "latest");
    assert.equal(page.text("#u-auto"), "on");
    assert.match(page.text("#u-binary"), /\.local\/bin\/claude/);
    assert.equal(page.text("#u-install"), "Reinstall latest");

    page.close();
});

test("the updates tab offers the version that is waiting", async () => {
    const page = await openSettings(
        {
            health: {
                ...HEALTH,
                available_version: "2.1.229",
                update_available: true,
            },
            version: {
                installed: "2.1.228",
                available: "2.1.229",
                channel: "latest",
                update_available: true,
                auto_update: true,
                binary: "/data/home/.local/bin/claude",
                last_update: { status: "idle" },
            },
        },
        "tab-updates",
    );

    assert.equal(page.text("#u-install"), "Update → 2.1.229");
    assert.equal(page.text("#u-available"), "2.1.229");
    assert.match(page.id("u-available").className, /is-ok/);

    page.close();
});

test("updating is out of reach while a job is running, and says why", async () => {
    const page = await openSettings({ health: { ...HEALTH, job_running: true } }, "tab-updates");

    assert.equal(page.id("u-install").disabled, true);
    assert.match(page.id("u-install").title, /job is running/);

    page.close();
});

test("an install in progress is reported in the tab and in the masthead", async () => {
    const page = await openSettings(
        {
            health: {
                ...HEALTH,
                updating: true,
                update: {
                    status: "running",
                    target: "latest",
                    previous: "2.1.228",
                    started_at: "2026-08-12T10:00:00+00:00",
                },
            },
        },
        "tab-updates",
    );

    assert.match(page.text("#u-progress"), /Installing latest from 2\.1\.228/);
    assert.match(page.id("u-progress").className, /progress--busy/);
    assert.equal(page.id("u-install").disabled, true);
    assert.equal(page.id("update-banner").hidden, false);
    assert.match(page.text("#update-text"), /Installing latest/);

    page.close();
});

test("a failed install shows the reason and what the CLI printed", async () => {
    const page = await openSettings(
        {
            health: {
                ...HEALTH,
                update: {
                    status: "failed",
                    target: "latest",
                    error: "claude install exited 1",
                    output: "curl: (6) Could not resolve host",
                    finished_at: "2026-08-12T10:00:30+00:00",
                },
            },
        },
        "tab-updates",
    );

    assert.match(page.text("#u-progress"), /failed: claude install exited 1/);
    assert.match(page.id("u-progress").className, /progress--bad/);
    assert.equal(page.id("u-output").hidden, false);
    assert.match(page.text("#u-output"), /Could not resolve host/);
    assert.match(page.id("update-banner").className, /banner--error/);

    page.close();
});

test("a finished install reports what moved, plugins included", async () => {
    const page = await openSettings(
        {
            health: {
                ...HEALTH,
                update: {
                    status: "done",
                    changed: true,
                    previous: "2.1.228",
                    installed: "2.1.229",
                    finished_at: "2026-08-12T10:00:30+00:00",
                    plugins: { marketplaces: "ok", plugins: { "some-plugin": "ok" } },
                },
            },
        },
        "tab-updates",
    );

    assert.match(page.text("#u-progress"), /Updated 2\.1\.228 → 2\.1\.229/);
    assert.match(page.text("#u-progress"), /Marketplaces and plugins refreshed/);

    page.close();
});

test("a plugin that did not survive the update is named", async () => {
    const page = await openSettings(
        {
            health: {
                ...HEALTH,
                update: {
                    status: "done",
                    changed: true,
                    previous: "2.1.228",
                    installed: "2.1.229",
                    finished_at: "2026-08-12T10:00:30+00:00",
                    plugins: { marketplaces: "ok", plugins: { "some-plugin": "failed" } },
                },
            },
        },
        "tab-updates",
    );

    assert.match(page.text("#u-progress"), /Plugins needing attention: some-plugin/);

    page.close();
});

test("pressing the install button starts one", async () => {
    const page = await openSettings({}, "tab-updates");

    await page.click(page.id("u-install"));

    const started = page.requests.find(
        (request) => request.method === "POST" && request.path === "update",
    );
    assert.notEqual(started, undefined);

    page.close();
});

test("checking now asks the release channel rather than the cache", async () => {
    const page = await openSettings({}, "tab-updates");
    const before = page.requests.length;

    await page.click(page.id("u-check"));

    const refreshed = page.requests
        .slice(before)
        .find((request) => request.path.startsWith("version?refresh"));
    assert.notEqual(refreshed, undefined);
    assert.equal(page.text("#u-check"), "check now");

    page.close();
});

test("a waiting update is announced in the masthead, since the button moved", async () => {
    const page = await bootConsole({
        health: { ...HEALTH, available_version: "2.1.229", update_available: true },
    });

    assert.equal(page.id("update-banner").hidden, false);
    assert.match(page.text("#update-text"), /2\.1\.229 is available/);
    assert.match(page.text("#update-text"), /Settings → Updates/);

    page.close();
});

test("the drawer names the magnifier, since a naked icon in a list says nothing", async () => {
    const page = await bootConsole();

    assert.match(page.text("#history"), /Past conversations/);

    page.close();
});

test("the actions collapse behind the menu button and open on demand", async () => {
    const page = await bootConsole({ chat: CHAT });

    assert.equal(page.id("drawer").className.includes("is-open"), false);
    assert.equal(page.id("menu").getAttribute("aria-expanded"), "false");

    await page.click(page.id("menu"));

    assert.equal(page.id("drawer").className.includes("is-open"), true);
    assert.equal(page.id("menu").getAttribute("aria-expanded"), "true");

    await page.press(page.document.body, "Escape");

    assert.equal(page.id("drawer").className.includes("is-open"), false);
    assert.equal(page.id("menu").getAttribute("aria-expanded"), "false");

    page.close();
});

test("choosing something from the menu puts it away", async () => {
    const page = await bootConsole();
    await page.click(page.id("menu"));

    await page.click(page.id("new-chat"));

    assert.equal(page.id("drawer").className.includes("is-open"), false);

    page.close();
});
