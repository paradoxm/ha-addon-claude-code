// Screenshots of the add-on's UI, so a layout can be judged rather than imagined.
// Drives the Chrome already installed on this machine — nothing is downloaded, and
// nothing here ships.
//
//     python3 tools/serve-ui.py &
//     node tools/shoot-ui.mjs [url] [outputDirectory]
//
// jsdom can drive the page but cannot show it, and every complaint about this UI so
// far has been about something only a rendered page reveals.

import { mkdirSync } from "node:fs";
import { join } from "node:path";

import puppeteer from "puppeteer-core";

const url = process.argv[2] || "http://127.0.0.1:8099/";
const outputDirectory = process.argv[3] || "/tmp/ui-shots";
mkdirSync(outputDirectory, { recursive: true });

const VIEWPORTS = {
    phone: { width: 390, height: 844, deviceScaleFactor: 2, isMobile: true },
    tablet: { width: 820, height: 1000, deviceScaleFactor: 1 },
    desktop: { width: 1440, height: 900, deviceScaleFactor: 1 },
    // The shapes that broke: a very short window, and a very narrow one.
    squat: { width: 1000, height: 380, deviceScaleFactor: 1 },
};

const browser = await puppeteer.launch({
    executablePath: "/usr/bin/google-chrome",
    args: ["--no-sandbox", "--disable-gpu", "--hide-scrollbars"],
});

async function shoot(page, name) {
    const path = join(outputDirectory, `${name}.png`);
    await page.screenshot({ path });
    console.log(path);
}

async function settle(page, ms = 900) {
    await new Promise((resolve) => setTimeout(resolve, ms));
}

for (const [name, viewport] of Object.entries(VIEWPORTS)) {
    const page = await browser.newPage();
    await page.setViewport(viewport);
    await page.goto(url, { waitUntil: "networkidle2" });
    await settle(page);
    await shoot(page, name);

    if (name === "phone") {
        await page.click("#menu");
        await settle(page, 500);
        await shoot(page, "phone-drawer");
        await page.click("#menu");
        await settle(page, 300);

        await page.click("#controls-chip");
        await settle(page, 400);
        await shoot(page, "phone-controls");
        await page.click("#controls-chip");
        await settle(page, 300);
    }

    if (name === "squat") {
        await page.close();
        continue;
    }

    if (name === "desktop") {
        await page.click("#settings-open");
        await settle(page, 500);
        for (const tab of ["usage", "updates", "mcp", "memory", "config"]) {
            await page.click(`#tab-${tab}`);
            await settle(page, 700);
            await shoot(page, `settings-${tab}`);
        }
    }

    await page.close();
}

await browser.close();
