// The three tabs that came last: the MCP switches, the instructions file, and the
// CLI's own configuration.

import assert from "node:assert/strict";
import test from "node:test";

import { bootConsole } from "./console.mjs";

const SERVERS = {
    servers: [
        {
            name: "playwright",
            scope: "local",
            transport: "stdio",
            summary: "npx --yes @playwright/mcp@latest",
            enabled: true,
        },
        {
            name: "homeassistant",
            scope: "user",
            transport: "stdio",
            summary: "uvx ha-mcp@latest",
            enabled: true,
        },
        {
            name: "sonarqube",
            scope: "user",
            transport: "http",
            summary: "https://sonar.example.com/mcp",
            enabled: false,
        },
    ],
    config_path: "/data/home/.claude.json",
    project_path: "/data/chat/.mcp.json",
};

async function openTab(id, answers = {}) {
    const page = await bootConsole(answers);
    await page.click(page.id("settings-open"));
    await page.click(page.id(id));
    await page.settle();
    return page;
}

test("the MCP tab groups servers by where they apply", async () => {
    const page = await openTab("tab-mcp", { mcp: SERVERS });

    const headings = [...page.document.querySelectorAll("#mcp-list h3")].map((node) =>
        node.textContent.trim(),
    );
    assert.equal(headings.length, 2);
    assert.match(headings[0], /^Everywhere/);
    assert.match(headings[1], /^This folder only/);

    const names = [...page.document.querySelectorAll(".mcp__name")].map((node) =>
        node.textContent,
    );
    assert.deepEqual(names, ["homeassistant", "sonarqube", "playwright"]);

    page.close();
});

test("a switched-off server reads as off and says so on the switch", async () => {
    const page = await openTab("tab-mcp", { mcp: SERVERS });

    const rows = [...page.document.querySelectorAll(".mcp")];
    const off = rows.find((row) => row.textContent.includes("sonarqube"));
    assert.match(off.className, /mcp--off/);
    assert.equal(off.querySelector("input").checked, false);
    assert.match(off.querySelector("input").getAttribute("aria-label"), /is off/);

    const on = rows.find((row) => row.textContent.includes("homeassistant"));
    assert.equal(on.className.includes("mcp--off"), false);
    assert.equal(on.querySelector("input").checked, true);

    page.close();
});

test("each server says how it is reached", async () => {
    const page = await openTab("tab-mcp", { mcp: SERVERS });

    const row = [...page.document.querySelectorAll(".mcp")].find((node) =>
        node.textContent.includes("sonarqube"),
    );

    assert.equal(row.querySelector(".pill").textContent, "http");
    assert.equal(row.querySelector(".mcp__summary").textContent, "https://sonar.example.com/mcp");

    page.close();
});

test("switching one off asks the add-on to switch it off", async () => {
    const page = await openTab("tab-mcp", {
        mcp: SERVERS,
        "mcp/homeassistant": { name: "homeassistant", enabled: false, changed: true },
    });
    const row = [...page.document.querySelectorAll(".mcp")].find((node) =>
        node.textContent.includes("homeassistant"),
    );
    const input = row.querySelector("input");

    input.checked = false;
    input.dispatchEvent(new page.window.Event("change", { bubbles: true }));
    await page.settle();

    const sent = page.requests.find((request) => request.path === "mcp/homeassistant");
    assert.equal(sent.method, "POST");
    assert.deepEqual(JSON.parse(sent.body), { enabled: false });

    page.close();
});

test("a switch the add-on refuses is reported and the list put back", async () => {
    const page = await openTab("tab-mcp", {
        mcp: SERVERS,
        "mcp/homeassistant": new Error("the CLI would not remove homeassistant"),
    });
    const input = [...page.document.querySelectorAll(".mcp")]
        .find((node) => node.textContent.includes("homeassistant"))
        .querySelector("input");

    input.checked = false;
    input.dispatchEvent(new page.window.Event("change", { bubbles: true }));
    await page.settle();

    assert.match(page.text("#error-banner"), /would not remove/);
    const again = [...page.document.querySelectorAll(".mcp")]
        .find((node) => node.textContent.includes("homeassistant"))
        .querySelector("input");
    assert.equal(again.checked, true, "the list is re-read, so it shows the real state");

    page.close();
});

test("nothing configured says so rather than showing an empty box", async () => {
    const page = await openTab("tab-mcp", { mcp: { servers: [] } });

    assert.equal(page.id("mcp-empty").hidden, false);
    assert.equal(page.document.querySelectorAll(".mcp").length, 0);

    page.close();
});

test("the instructions file is shown with its path and saved as typed", async () => {
    const page = await openTab("tab-memory", {
        "files/memory": {
            key: "memory",
            path: "/data/home/.claude/CLAUDE.md",
            kind: "markdown",
            exists: true,
            text: "Answer in Russian.\n",
        },
    });

    assert.equal(page.text("#memory-path"), "/data/home/.claude/CLAUDE.md");
    assert.equal(page.id("memory-text").value, "Answer in Russian.\n");

    page.id("memory-text").value = "Answer in Russian. Keep it short.\n";
    page.id("memory-text").dispatchEvent(new page.window.Event("input", { bubbles: true }));
    await page.click(page.id("memory-save"));

    const saved = page.requests.find((request) => request.method === "PUT");
    assert.equal(saved.path, "files/memory");
    assert.equal(saved.body, "Answer in Russian. Keep it short.\n");
    assert.equal(page.text("#memory-status"), "Saved.");

    page.close();
});

test("a file that has not been written yet says so and can still be saved", async () => {
    const page = await openTab("tab-memory", {
        "files/memory": {
            key: "memory",
            path: "/data/home/.claude/CLAUDE.md",
            kind: "markdown",
            exists: false,
            text: "",
        },
    });

    assert.match(page.text("#memory-path"), /not written yet/);
    assert.equal(page.id("memory-save").disabled, false);

    page.close();
});

test("the config tab refuses to save broken JSON and says what is wrong", async () => {
    const page = await openTab("tab-config", {
        "files/config": {
            key: "config",
            path: "/data/home/.claude.json",
            kind: "json",
            exists: true,
            text: '{"mcpServers": {}}',
        },
    });
    assert.equal(page.text("#config-status"), "Valid JSON.");

    page.id("config-text").value = "{ truncated";
    page.id("config-text").dispatchEvent(new page.window.Event("input", { bubbles: true }));
    await page.settle();

    assert.equal(page.id("config-save").disabled, true);
    assert.match(page.id("config-status").className, /hint--bad/);
    assert.equal(
        page.requests.some((request) => request.method === "PUT"),
        false,
    );

    page.close();
});

test("format reindents the config without sending it anywhere", async () => {
    const page = await openTab("tab-config", {
        "files/config": {
            key: "config",
            path: "/data/home/.claude.json",
            kind: "json",
            exists: true,
            text: '{"a":{"b":1}}',
        },
    });

    await page.click(page.id("config-format"));

    assert.match(page.id("config-text").value, /\n {2}"a": \{/);
    assert.equal(
        page.requests.some((request) => request.method === "PUT"),
        false,
    );

    page.close();
});

test("a save the add-on refuses leaves the reason on the page", async () => {
    const page = await openTab("tab-config", {
        "files/config": {
            key: "config",
            path: "/data/home/.claude.json",
            kind: "json",
            exists: true,
            text: "{}",
        },
    });

    // The next request to this path fails, the way a save during a run does.
    page.routes["files/config"] = new Error("Claude is working; this file is its own");
    await page.click(page.id("config-save"));

    assert.match(page.text("#config-status"), /Claude is working/);
    assert.match(page.id("config-status").className, /hint--bad/);

    page.close();
});

test("a file too large for the editor says to use the terminal", async () => {
    const page = await openTab("tab-config", {
        "files/config": new Error("/data/home/.claude.json is 4096 kb; too large to edit here"),
    });

    assert.match(page.text("#config-status"), /too large to edit here/);
    assert.equal(page.id("config-save").disabled, true);

    page.close();
});
