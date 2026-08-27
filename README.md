# tnotes

Everything [`tdiff`](https://github.com/brunnorpdias/tdiff) and
[`tcat`](https://github.com/brunnorpdias/tcat) both need to read an Obsidian vault of
task notes. One module, one copy, imported by both.

## Why it exists

Both tools read the same vault, the same notes and the same notation. They used to say so
by **vendoring**: `tcat` carried a marked copy of `tdiff`'s task core and a check script
compared it against a pinned commit. That failed the way vendoring always does — the pin
went stale, `parse_note` and `clean_text` drifted, and the check never covered
`materialize` or `load_config` at all, so the two tools quietly disagreed about which
status a deduped task carries and about what a missing config means.

One copy cannot drift. The check script is deleted.

## What is in it

Name normalisation, note parsing, the vault reader (with its stall handling), config
loading, the date and week core, and clustering/dedup. What stays in each tool is what is
genuinely its own: the diff, the grouping, the rendering, the argument parsers.

**Nothing here knows a vault's vocabulary.** Not a section name, not a day-marker
spelling, not a tag, not the character that marks a comment — those are the config's to
say. What it does own is the notation's *shape*: that a task line looks like
`- [x] name`, that a heading bounds a section, that a Sun–Sat week is named for the year
it ends in.

**And every matcher here matches the thing it names, and nothing else.** A day marker is
a line that *is* a marker: `[days]` values are literals, anchored at both ends, with
`{date}` as the sole placeholder — and a task line is tested for first, so it is never
mistaken for one. A comment separator is a character the vault nominates, and only counts
between spaces and outside every bracket and parenthesis. Two tasks are the same task iff
their names are identical after `clean_text`, ignoring case; `cluster_records()` holds no
similarity metric. Each of those replaced something looser that had been silently
throwing tasks away — see `tdiff`'s CLAUDE.md for the measurements.

## Install

No packaging, no install step. Symlink it the same way the scripts are symlinked onto
`$PATH`:

```sh
mkdir -p ~/.local/lib
ln -s ~/Projects/tnotes/tnotes.py ~/.local/lib/tnotes.py
```

Each tool puts that directory on `sys.path` itself and reports a missing symlink in a
sentence rather than as an `ImportError` traceback.

## Config

Also shared, in `~/.config/tconfig/`, split by concern rather than by tool — that being
the axis along which a file actually changes:

```sh
mkdir -p ~/.config/tconfig
cp notation.example.toml ~/.config/tconfig/notation.toml
cp statuses.example.toml ~/.config/tconfig/statuses.toml
```

- **`notation.toml`** — how the vault writes things: `[exclude]`, `[days]`, `[comment]`, `[vault]`
- **`statuses.toml`** — what the statuses mean: `[order]`, `[dedup]`, `[roles]`, `[theme.*]`
- **`<tool>.toml`** — optional per-tool overrides, then `$TDIFF_CONFIG`/`$TCAT_CONFIG`, then `--config`

Each layer *merges* over the ones below (tables merge, lists replace wholesale).
`$TCONFIG_DIR` relocates the folder. Nothing is ever bootstrapped.

## Usage

```python
sys.path.insert(0, str(Path.home() / '.local' / 'lib'))
import tnotes as tn
tn.init('tdiff')
```

`init()` names the calling tool: every message the module writes is prefixed with it, and
`config_paths()` reads it to find that tool's overlay. The environment variables are the
module's rather than either tool's — `TNOTES_WORKERS` (default 4, `1` = serial),
`TNOTES_TIMEOUT` (seconds, default 1), `TNOTES_DEBUG=1` — because they are read at import,
before `init()` has been called, and because a vault reader that behaved differently per
tool would be a fresh way for the two to disagree.

## License

MIT. See [LICENSE](LICENSE).
