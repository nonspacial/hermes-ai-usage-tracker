# Install the cache update — plugin files only

**Use this version instead of the previous seven-step checklist. Start from step 1 below; do not resume an old numbered step.**

This procedure installs the snapshot and refresh correction `3d723f6b7308f246ba259b27da07ae24ba66e92d` into **default and infra**. It does **not** open, back up, migrate, repair or restore either usage database. It does not change credentials, enable plugins, restart services or update Hermes itself.

The previous attempt migrated default but refused infra, and its shell wrapper incorrectly printed success. **Keep `/home/nope/ai-usage-offline-8Dxh8rLk/` and its backups unchanged. Do not restore those databases.** This procedure leaves that database state alone.

The new renderer can cache separate range snapshots without migrating infra. Infra will still use full reads for background updates; this is **not completion of the incremental-refresh migration**. Cache entries remain subject to the existing memory/age limits and scope checks. A cold first load can still be slow.

After reopening, selected-profile reads use the approved SQLite-managed backup to temporary storage rather than rejecting raw file copies whenever recording changes the ledger. SQLite may manage its normal WAL/SHM support files; usage records are not edited by the reader. All-profile reads keep their stricter existing copy method. This release also releases the manual Refresh control after its read completes and no longer labels a usage-read error as a disconnected recorder when recorder health is good.

## 1. Save this file and prepare a separate terminal

Use this exact file in your editor:

`/mnt/vdo_storage/Projects/codex_usage_export_kit/docs/OFFLINE_STEPS_SIMPLE.md`

Open a normal desktop terminal, **not the terminal inside Hermes**. Run this line on its own:

```bash
bash --noprofile --norc
```

A `bash-5.3$`-style prompt is normal. Use a fresh shell: the old `run_step`, `AU_LAST_PASS` and `PIPESTATUS` handling are no longer used.

**Working folder:** the project's main folder, not `.hermes`. The installation block below enters `/mnt/vdo_storage/Projects/codex_usage_export_kit` itself and prints that folder. The Python command uses an absolute path, so your terminal's starting folder does not matter.

**If Bash or the project Python is missing:** stop and ask for help. Do not install packages, use sudo or substitute a different interpreter.

## 2. Fully quit Hermes before replacing plugin files

Fully **Quit** Hermes Desktop, including its tray instance. Stop your other Hermes CLI/gateway/agent sessions that use these two local profiles normally, and keep them stopped during installation. Do not merely minimise the window. If a supervisor restarts them automatically and you do not know how to pause it, ask before proceeding. Do not stop unrelated remote gateways.

There is **no database migration or `/proc` open-file scan in this procedure**. The previous scan's permission errors are not being treated as proof that writers are stopped. This step prevents code loading/hot-reload during a multi-file plugin replacement; it is not a substitute for the stronger writer-exclusion checks required by a future database migration.

**If you cannot stop the relevant sessions:** do not run step 3. Reopening Hermes to ask for help is fine; nothing has been installed yet.

Before starting step 3, keep this editor and terminal available. If installation is interrupted after file replacement starts, obtain help through a separate browser/chat/device without launching an uncertain plugin build. If that is not available, ask for an assisted recovery arrangement before starting. There is no blanket safe restart command for a half-written installation.

## 3. Install and verify both profiles

Copy **this entire block**, including the final `PYCODE` line, but not the Markdown backticks. Press Enter if your terminal leaves the pasted block waiting. Do not paste the next instructions into an unfinished command.

This single Python process checks the source and both targets, previews both plans, installs each profile and verifies installed bytes and rollback receipts. **It does not depend on a shell pipeline to decide success.** Installer detail is saved to the printed log; progress appears in the terminal. Wait for the final line.

```bash
/mnt/vdo_storage/Projects/codex_usage_export_kit/.venv/bin/python -B - <<'PYCODE'
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

REPO = Path('/mnt/vdo_storage/Projects/codex_usage_export_kit')
COMMIT = '3d723f6b7308f246ba259b27da07ae24ba66e92d'
HOMES = (('default', Path('/home/nope/.hermes')),
         ('infra', Path('/home/nope/.hermes/profiles/infra')))
run = None
log = None
stage = 'setup'
started = False

def require(ok, message):
    if not ok:
        raise RuntimeError(message)

def say(message):
    # Write and flush the log BEFORE announcing success or starting a write.
    log.write(message + '\n')
    log.flush()
    print(message, flush=True)

def git(*args):
    return subprocess.check_output(['git', *args], cwd=REPO)

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def command(args):
    log.flush()
    result = subprocess.run(args, cwd=REPO, stdout=log, stderr=subprocess.STDOUT)
    require(result.returncode == 0,
            'Installer command failed with exit ' + str(result.returncode))
    log.flush()

try:
    os.umask(0o077)
    os.chdir(REPO)
    run = Path(tempfile.mkdtemp(prefix='ai-usage-plugin-install-', dir=Path.home()))
    log = (run / 'terminal.log').open('x', buffering=1)
    say('Folder: ' + str(Path.cwd()))
    say('Log: ' + str(run / 'terminal.log'))
    stage = 'source and target checks (no installed files changed)'
    require(git('rev-parse', 'HEAD').decode().strip() == COMMIT,
            'Wrong source commit. Do not reset or switch it blindly; ask for help.')
    changes = git('status', '--porcelain=v1', '-z', '--untracked-files=all').decode().split('\0')
    require(all(not row or (row[3:] == 'docs/OFFLINE_STEPS_SIMPLE.md'
                           and 'R' not in row[:2] and 'C' not in row[:2]) for row in changes),
            'Unexpected checkout changes. Run git status --short; preserve your files.')
    require((REPO / 'install.py').read_bytes() == git('show', COMMIT + ':install.py'),
            'Installer does not match the accepted commit.')
    import install
    require(Path(install.__file__).resolve() == REPO / 'install.py',
            'Wrong installer module loaded.')
    plans = {}
    for label, home in HOMES:
        _, plans[label] = install.plan(home)
        require(bool(plans[label]), label + ': empty installer plan')
        for src, dst in plans[label]:
            require(src.read_bytes() == git('show', COMMIT + ':' + str(src.relative_to(REPO))),
                    'Source file differs from committed bytes: ' + str(src))
            require(not dst.exists() or dst.is_file(), 'Not a regular destination: ' + str(dst))
        say('SOURCE VERIFIED: ' + label + ' (' + str(len(plans[label])) + ' files)')
    stage = 'installation preview (no installed files changed)'
    for label, home in HOMES:
        command([sys.executable, '-B', 'install.py', '--home', str(home)])
        say('PREVIEW VERIFIED: ' + label)
    for label, home in HOMES:
        stage = 'installing/verifying ' + label
        parent = home / 'usage-ledger-backups'
        before = set(parent.iterdir()) if parent.is_dir() else set()
        say('INSTALL STARTING: ' + label + '; receipts under ' + str(parent))
        started = True
        try:
            command([sys.executable, '-B', 'install.py', '--home', str(home), '--apply'])
        finally:
            new = set(parent.iterdir()) - before if parent.is_dir() else set()
            for folder in sorted(new):
                say('RECEIPT: ' + str(folder / 'receipt.json'))
        require(len(new) == 1, label + ': expected one new receipt folder')
        receipt = next(iter(new)) / 'receipt.json'
        record = json.loads(receipt.read_text())
        require(record['status'] == 'installed' and record['home'] == str(home)
                and record['version'] == 1, label + ': receipt status/target mismatch')
        items = {item['relative_path']: item for item in record['files']}
        plan = plans[label]
        require(len(items) == len(record['files']) == len(plan)
                and set(items) == {str(dst.relative_to(home)) for _, dst in plan},
                label + ': receipt file list mismatch')
        for src, dst in plan:
            item = items[str(dst.relative_to(home))]
            committed = git('show', COMMIT + ':' + str(src.relative_to(REPO)))
            install.no_links(dst)
            require(src.read_bytes() == dst.read_bytes() == committed,
                    'Installed bytes differ: ' + str(dst))
            require(sha(dst) == item['installed_sha256'], 'Receipt hash differs: ' + str(dst))
            if item['backup'] is None:
                require(item['old_sha256'] is None, 'Unexpected old-file hash: ' + str(dst))
            else:
                old = receipt.parent / item['backup']
                require(old.resolve(strict=True) == old and old.is_relative_to(receipt.parent)
                        and sha(old) == item['old_sha256'], 'Plugin backup failed verification: ' + str(old))
        say('INSTALL VERIFIED: ' + label)
    stage = 'final verification of both homes'
    for label, home in HOMES:
        for src, dst in plans[label]:
            install.no_links(dst)
            require(dst.read_bytes() == git('show', COMMIT + ':' + str(src.relative_to(REPO))),
                    'Final installed-file mismatch: ' + str(dst))
    say('PASS: BOTH PLUGIN INSTALLATIONS VERIFIED. You may reopen Hermes.')
except BaseException as exc:
    message = 'FAIL during ' + stage + ': ' + type(exc).__name__ + ': ' + str(exc)
    print(message, file=sys.stderr, flush=True)
    if log is not None:
        try:
            log.write(message + '\n')
            log.flush()
        except OSError:
            print('Logging also failed. Keep this terminal output.', file=sys.stderr)
    if run is not None:
        print('Keep the run folder: ' + str(run), file=sys.stderr)
        try:
            print('--- Last log lines ---', file=sys.stderr)
            print('\n'.join((run / 'terminal.log').read_text().splitlines()[-25:]), file=sys.stderr)
        except OSError:
            pass
    print('Plugin files may be partly changed. Do not rerun or restore databases; use the failure guidance.'
          if started else 'No installation was started. You may reopen Hermes for help.', file=sys.stderr)
    raise SystemExit(1)
finally:
    if log is not None:
        log.close()
PYCODE
```

### Expected output

The log folder and receipt names vary. These are the significant lines, **not results already observed on your profiles**:

```text
Folder: /mnt/vdo_storage/Projects/codex_usage_export_kit
Log: /home/nope/ai-usage-plugin-install-.../terminal.log
SOURCE VERIFIED: default (... files)
SOURCE VERIFIED: infra (... files)
PREVIEW VERIFIED: default
PREVIEW VERIFIED: infra
INSTALL STARTING: default; receipts under ...
RECEIPT: .../receipt.json
INSTALL VERIFIED: default
INSTALL STARTING: infra; receipts under ...
RECEIPT: .../receipt.json
INSTALL VERIFIED: infra
PASS: BOTH PLUGIN INSTALLATIONS VERIFIED. You may reopen Hermes.
```

**Only that final `PASS: BOTH PLUGIN INSTALLATIONS VERIFIED` authorises the normal restart in step 4.** `PREVIEW VERIFIED`, an installer's `Installed`, or one profile's `INSTALL VERIFIED` is not completion. Keep both receipt folders and the new log folder.

### If it does not pass

| What you see | What to do |
|---|---|
| No `Folder:` line; missing interpreter/path, syntax error or a continuation prompt | The Python procedure has not started successfully. If the shell is waiting for the rest of the pasted block, press Ctrl+C rather than pasting another command into it. Preserve the output and reopen Hermes for help. |
| `FAIL during source and target checks` or `installation preview` | No `--apply` has run. Reopen Hermes for help. Preserve changed files; do not reset Git, delete files or change ownership to bypass the check. |
| `FAIL during installing/verifying ...` | Some plugin files may have changed. Keep the log and receipt folders; do not blindly rerun the installer or reopen the affected profiles. Use the separate help route arranged in step 2. |
| `FAIL during final verification`, a missing receipt, or an installed-byte/backup mismatch | Installation is unverified. Preserve everything and obtain a receipt-specific repair decision before restarting. |
| Disk full, permission denied or logging failure | Stop. Keep terminal output if logging failed. Whether reopening is safe depends on whether `INSTALL STARTING` was reached; use the corresponding row above. Do not delete ledger or backup files. |
| Terminal closed, power loss or an interrupted command after `INSTALL STARTING` | Treat it as a partial installation, not a pass. Preserve the printed run/receipt folders and seek recovery help. |

The failure handler exits unsuccessfully and prints `FAIL`; it cannot turn a failed installer command into the final success message. If the interpreter itself is killed, there may be no failure message: **absence of the final pass is not success**.

For help, send the terminal output or the new `terminal.log` shown at the top. Open it in your editor; no shell variables are needed. **Do not send database files or credentials. Do not restore the old database backups.** Plugin-file recovery and database recovery are separate operations.

## 4. Reopen and check range caching

After the final pass, reopen Hermes normally. No enable command is needed for these already-enabled profiles.

1. Open the same provider/profile and let **24h** fully load.
2. Switch to **7 days** and let it fully load.
3. Return to **24h**. Hover or focus the connection badge: an eligible retained snapshot should show **Updating… · cached** with a coloured elapsed age in the tooltip while fresh data is fetched. The badge's existing recorder explanation remains above it.
4. Set a valid **Custom** range and let it load. Switch away, let the other range load, then return to the unchanged Custom range.
5. Changing Custom's bounds selects different data; it must not display the previous range as if it matched.

Perform this promptly: the current cache has a five-minute age limit, twelve-entry limit, 8 MiB total serialized-payload budget and 2 MiB per-entry limit. It is not unlimited retention until exit. Full quit clears it. A missing eligible snapshot still requires a fresh read; this installation does not promise every payload can be cached or every refresh will be instant.

If a return still loads from scratch, report the range sequence, approximate wait and whether **Updating… · cached** appeared in the connection badge tooltip. Do not repeat installation or migration just because a performance check fails.

The original [offline migration runbook](OFFLINE_DELIVERY.md) is unchanged. **Do not run its migration sequence as part of this plugin-only retry.** Remaining infra migration work is separate.
