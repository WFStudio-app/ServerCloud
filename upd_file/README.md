# upd_file — Auto-Update marker directory

Plain `.txt` / `.data` files that the **Settings → Auto-Update** functions
fetch from GitHub (raw.githubusercontent.com) and parse. They describe what
can be updated, which version is current, and whether an update is urgent.

| File                 | Read by            | Content |
|----------------------|--------------------|---------|
| `kernel_version.txt` | Kernel Update      | single-line kernel version label (THE version marker) |
| `kernel_release.data`| Kernel Update      | key = value: `version`, `urgent`, `urgent_reason`, `security`, `languages`, `description`, `date` |
| `security.data`      | Security Update    | key = value: `version`, `urgent`, `urgent_reason`, `severity`, `languages`, `description`, `date` |
| `os_latest.txt`      | Check OS Updates   | latest release commit summary (`commit`, `subject`, `author`, `date`) |

Urgency rule (kernel): exactly three reasons are allowed —
`security_update`, `vulnerability_found`, `critical_bug_fix`.
An urgent record is shown in the UI with a red `[!]`.

Canonical source of truth lives next to the code:
`kernel/VERSION.data`, `kernel/RELEASE.data`, `security/SECURITY.data`.
This directory is a mirror; regenerate it after every bump:

```
python -m truck_modules.sync_upd_file
```

The engine reads `upd_file/` first and falls back to the canonical copies.
All remote data is treated as untrusted: size-capped, version-validated,
string-truncated before display.
