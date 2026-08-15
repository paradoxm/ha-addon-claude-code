# tools

Development helpers. None of these ship inside the add-on image, which copies
`www/`, `api.py` and the two shell scripts and installs no Node runtime.

## Tests

The add-on's own tests live in `../tests`. Nothing has to be installed for the
Python ones beyond pytest:

```bash
python3 -m pytest tests -q          # the API, over HTTP, with a stand-in claude
npm install && npm test             # the UI, in a DOM, with a stand-in API
```

`tests/conftest.py` sets up a throwaway `/data`, puts `stub-claude.py` on `PATH` as
`claude`, and starts the real server on a port the OS picks. The stand-in answers
the invocations the add-on makes and, for a run, produces genuine `stream-json`
output and a session transcript in the layout Claude Code uses — including the
records the real CLI writes for a slash command, which is what makes the chat, the
history, streaming, cancellation and the filtering of the CLI's own bookkeeping
testable without an account. Its behaviour is steered by `STUB_SLEEP`, `STUB_FAIL`,
`STUB_NOTICE`, `STUB_API_ERROR`, `STUB_VERSION`, `STUB_INSTALL_TO` and
`STUB_INSTALL_FAIL`.

`tests/frontend` loads the real `index.html`, `style.css` and `app.js` into jsdom
and drives them the way a person does. Node's own test runner, one dev dependency,
no framework and no build step — the UI is three static files on purpose.

Line coverage of `api.py`:

```bash
python3 -m coverage run --source=claude-code -m pytest tests -q
python3 -m coverage report --show-missing
```

That is 100% of statements and 99% of branches, from 273 tests. The UI has 49 of
its own. The two original
standard-library check scripts that used to live here have been retired: everything
they covered is in `tests/`, and keeping both meant two places to update.

## Linting

```bash
python3 -m ruff check          # configured in ruff.toml
python3 -m mypy claude-code/api.py tests
npm run lint                   # configured in eslint.config.mjs
```

Both are configured explicitly rather than left on their defaults, so a `noqa` has
to name a rule that is switched on — an unnecessary one is itself an error, which is
what stops them accumulating as decoration.

## Looking at it

```bash
python3 tools/serve-ui.py            # http://127.0.0.1:8099
node tools/shoot-ui.mjs              # screenshots to /tmp/ui-shots
```

`serve-ui.py` runs the real `api.py` against a throwaway `/data` with the stand-in
CLI, and serves `www/` in front of it the way nginx does in the image.
`shoot-ui.mjs` drives the Chrome already installed on this machine — phone, tablet
and desktop, plus the drawer and the updates tab. jsdom can drive the page but
cannot show it, and every complaint about this UI so far has been about something
only a rendered page reveals.

## Icons

```bash
python3 tools/make-icons.py     # regenerates claude-code/icon.png and logo.png
```
