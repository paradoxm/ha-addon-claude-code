// The rules the UI is held to. Flat config, no framework plugins: the page is three
// static files the image copies verbatim, and nothing here runs at build time.

import js from "@eslint/js";
import globals from "globals";

export default [
    {
        files: ["claude-code/www/*.js"],
        languageOptions: {
            ecmaVersion: 2023,
            sourceType: "script",
            globals: globals.browser,
        },
        rules: {
            ...js.configs.recommended.rules,
            eqeqeq: ["error", "always"],
            "no-var": "error",
            "prefer-const": "error",
            "no-implicit-globals": "error",
            "no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
        },
    },
    {
        files: ["tests/frontend/**/*.mjs"],
        languageOptions: {
            ecmaVersion: 2023,
            sourceType: "module",
            globals: globals.node,
        },
        rules: js.configs.recommended.rules,
    },
];
