# KinoWatch TUI

A simple terminal client for an account authorized to download content from `kino.watch`.

## Install and run

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .
./run-tui
```

On first start, enter your login and password. The password is used only for that request. Session cookies are stored locally at `~/.local/state/kinowatch/session.json` (or `$XDG_STATE_HOME/kinowatch/session.json`) with owner-only permissions, and are reused on later launches.

Answer the login prompt, and if the site sends an email verification code, enter that code when asked. Then search a title, select a result with the arrow keys (or `j`/`k`), choose an exposed direct media file, and download. Downloads go to `./downloads` and resume from an existing `.part` file when the server supports byte ranges.

The client uses only a source URL exposed to the authenticated page. It does not circumvent DRM, access controls, challenges, or protected streaming mechanisms; pages that do not expose a direct file report that fact.
