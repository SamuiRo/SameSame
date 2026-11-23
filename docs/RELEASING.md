# Releasing SameSame

A release is built locally on Windows and published with the same files to
GitHub (`SamuiRo/SameSame`) and Forgejo on starbase (`StarLith/SameSame`).
It is built locally rather than in CI because starbase is reachable only on
the LAN, and a Windows build cannot be produced on a Linux runner.

## One-time setup

```powershell
winget install JRSoftware.InnoSetup
gh auth login
```

For Forgejo, create an access token with the `write:repository` scope
(*Settings > Applications* on http://starbase:1200), then set it for the session.
The Forgejo address defaults to `http://starbase:1200`; override it with
`$env:SAMESAME_FORGEJO_URL` if it ever moves.

```powershell
$env:FORGEJO_TOKEN = "<token>"
```

Do not commit the token.

## Release steps

1. Bump the version in both `pyproject.toml` and `dedupe/__init__.py`, update
   `docs/STATUS.md`, and commit (`vX.Y.Z <summary>`, as usual).
2. Run:

   ```powershell
   .\tools\release.ps1
   ```

   It refuses a dirty tree or an existing tag, builds with
   `tools\build_windows.ps1`, tags `vX.Y.Z`, pushes the tag (origin pushes to
   GitHub and starbase), and creates both releases with:

   - `SameSame-X.Y.Z-win64-setup.exe`
   - `SameSame-X.Y.Z-win64.zip`
   - `SHA256SUMS-X.Y.Z.txt`

   Pass `-NotesFile notes.md` for hand-written notes; by default the notes are
   the release commit message plus install instructions.

3. If Forgejo was skipped or failed, finish it without rebuilding:

   ```powershell
   .\tools\release.ps1 -ForgejoOnly
   ```

## Building without releasing

```powershell
.\tools\build_windows.ps1
```

The output lands in `dist\`. `build\` holds the throwaway build virtualenv and
PyInstaller work files; both folders are git-ignored. Use `-Reuse` to keep the
build virtualenv between runs and `-SkipInstaller` to build only the zip.

## How the build is put together

| File | Role |
| --- | --- |
| `packaging/windows/samesame.spec` | PyInstaller: one folder with `SameSame.exe` (windowed GUI) and `samesame-cli.exe` (console CLI) sharing one runtime |
| `packaging/windows/entry_gui.py`, `entry_cli.py` | Frozen entry points |
| `packaging/windows/samesame.iss` | Inno Setup installer and uninstaller |
| `tools/build_windows.ps1` | Clean venv, freeze, smoke test, zip, installer, checksums |
| `tools/release.ps1` | Tag, push, GitHub and Forgejo releases |

Decisions worth keeping:

- **One folder, not `--onefile`.** A one-file build unpacks itself to a temp
  folder on every launch: slow starts, leftover temp files, and more antivirus
  false positives.
- **The CLI is `samesame-cli.exe`.** Windows file names are case-insensitive,
  so `samesame.exe` would collide with `SameSame.exe` in the same folder.
- **FFmpeg is not bundled.** It is large and GPL-licensed; the GUI reports on
  startup when it is missing.
- **Per-user install, no admin prompt.** The installer's `AppId` must never
  change, or upgrades and uninstall entries break.
- **Uninstall cleanup.** The program is removed; settings
  (`HKCU\Software\SameSame`), cache and journal (`%LOCALAPPDATA%\SameSame`) are
  removed only if the user agrees; the quarantine folder is never removed.
- **Not code-signed.** SmartScreen shows an unknown-publisher warning on first
  run.
