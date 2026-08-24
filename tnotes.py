"""Everything tdiff and tcat both need to read an Obsidian vault of task notes.

Both tools read the same vault, the same notes and the same notation. They used to
say so by *vendoring*: tcat carried a marked copy of this code and a check script
compared it against a pinned tdiff commit. That failed the way vendoring always does —
the pin went stale, parse_note and clean_text drifted, and the check never covered
materialize or load_config at all, so the two quietly disagreed about which status a
deduped task carries and about what a missing config means. One copy cannot drift.

Nothing here knows a vault's vocabulary. Not a section name, not a day-marker
spelling, not a tag: those are the config's to say, and this module's job is to have
no opinion about them. What it does own is the notation's *shape* — that a task line
looks like `- [x] name`, that a heading bounds a section, that a Sun-Sat week is named
for the year it ends in.

Import it as:

    sys.path.insert(0, str(Path.home() / '.local' / 'lib'))
    import tnotes as tn
    tn.init('tdiff')

init() names the calling tool: every message written here is prefixed with it, and
config_paths() reads it to find that tool's overlay. The environment variables are the
module's rather than the tool's (TNOTES_WORKERS, TNOTES_TIMEOUT, TNOTES_DEBUG) because
they are read at import, before init() has been called — and because a vault reader
that behaved differently per tool would be a fresh way for the two to disagree.
"""

import sys, re, os, time, threading, subprocess, tomllib, fnmatch
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

_SEP_RE = re.compile(r' [-–—] ')  # hyphen, en-dash, em-dash

def strip_section_suffix(s):
    """Remove trailing ' – ...' only when the dash is outside of [...] brackets."""
    depth = 0
    for i, c in enumerate(s):
        if c == '[':
            depth += 1
        elif c == ']':
            depth = max(0, depth - 1)
        elif depth == 0 and c == ' ' and _SEP_RE.match(s, i):
            return s[:i].strip()
    return s.strip()

# Populated by load_config() once args are parsed. Placeholder empty values here
# so nothing explodes if referenced before load_config() runs.
STATUS_PRIORITY  = {}

DISPLAY_ORDER    = {}

SETTLED_STATUSES = set()

HIDDEN_STATUSES  = set()

PROJECT_STATUSES = set()

THEMES   = {}     # theme name -> {char: '#rrggbb'}; tcat paints, tdiff does not

FULL_ROW = set()  # statuses whose whole row takes the colour, not just the box

EXCLUDED_SECTIONS = ()

EXCLUDED_TAGS = ()

DAY_PATTERNS = ()

DAILY_FOLDER  = ''

WEEKLY_FOLDER = ''

CONFIG_FOUND  = False

# Complaints worth making but not worth interrupting for. Collected during the run
# and flushed to stderr at the end by flush_notices(), so they never interleave with
# the rows on stdout. Suppressed under --json and --no-summary, both of which mean
# "output with nothing around it".
TOOL = 'tnotes'

def init(tool):
    """Name the calling tool. Every message this module writes is prefixed with it,
    and config_paths() reads it to find the tool's own overlay. Call once, before
    load_config()."""
    global TOOL
    TOOL = tool

class DateError(Exception):
    """A date argument this module could not resolve. Raised rather than reported,
    because the message belongs to the caller's argument parser — which is the one
    thing a shared module cannot own. It is why resolve_date used to sit outside the
    vendored block and drift."""

NOTICES = []

def notice(msg):
    NOTICES.append(msg)

def _merge(base, overlay):
    """Recursive dict merge; the overlay wins.

    Lists replace wholesale rather than concatenating: an overlay that names a
    partial [dedup].priority means exactly those tiers, not an insertion into a
    lower layer's list.
    """
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v

def _xdg_base():
    xdg = os.environ.get('XDG_CONFIG_HOME')
    return Path(xdg) if xdg else Path.home() / '.config'

# The shared config folder. It belongs to neither tool, which is the whole point: one
# vault, one description of it, and no tool depending on the other being installed.
# Split by concern rather than by tool — notation.toml says how the vault writes
# things, statuses.toml says what the statuses mean — because that is the axis along
# which a file actually changes.
LEGACY_STATUS_DIR = 'obsidian-tasks'

def config_dir():
    env = os.environ.get('TCONFIG_DIR')
    return Path(env) if env else _xdg_base() / 'tconfig'

def config_paths(override):
    """Every config source, lowest precedence first.

    Each source *merges* over the ones below it, including $<TOOL>_CONFIG and
    --config. A partial file must never erase what a lower layer set: --config used to
    be a wholesale replacement, so pointing it at a file with no `statuses` table
    silently zeroed every priority and disabled both -I and the project exclusion.

    The pre-tconfig layout is still read when tconfig/ holds nothing, so the first run
    after the move is not a hard error. It is a fallback, not a layer: a tconfig/ that
    exists wins outright, or a half-migrated setup would silently merge two homes.
    """
    d = config_dir()
    paths = [d / 'notation.toml', d / 'statuses.toml', d / f'{TOOL}.toml']
    if not any(p.exists() for p in paths):
        base = _xdg_base()
        legacy = [base / LEGACY_STATUS_DIR / 'statuses.toml',
                  base / TOOL / 'config.toml']
        if any(p.exists() for p in legacy):
            notice(f'reading the old config layout; its new home is {d}')
            paths = legacy
    env = os.environ.get(f'{TOOL.upper()}_CONFIG')
    if env:
        paths.append(Path(env))
    if override:
        paths.append(Path(override))
    return paths

def load_config(override, error, required=True):
    """Merge every config layer and populate the module-level globals.

    Never bootstraps a file. `required` is the one place the two tools genuinely
    differ, and it is now an argument rather than an accident: tdiff cannot run
    usefully with no config at all — an empty PROJECT_STATUSES leaks project headers
    into every diff — so it passes True and no layers is a hard error. tcat degrades
    to unranked and uncoloured and says so. A layer that exists but omits a section
    degrades with a notice either way.

    [theme.*] is read by tcat and ignored by tdiff, where the diff type owns the row
    colour and a status colour would have nothing to paint. The merged table is the
    superset; each tool reads what it has a use for.
    """
    global STATUS_PRIORITY, DISPLAY_ORDER, SETTLED_STATUSES, HIDDEN_STATUSES
    global PROJECT_STATUSES, EXCLUDED_SECTIONS, EXCLUDED_TAGS, DAY_PATTERNS
    global THEMES, FULL_ROW, DAILY_FOLDER, WEEKLY_FOLDER
    global CONFIG_FOUND

    merged = {}
    for path in config_paths(override):
        if not path.exists():
            continue
        try:
            with path.open('rb') as f:
                data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            error(f'invalid config at {path}: {e}')
            return
        CONFIG_FOUND = True
        _merge(merged, data)

    if not CONFIG_FOUND:
        msg = ('no status config found — install statuses.example.toml to '
               f'{config_dir() / "statuses.toml"}')
        if required:
            error(msg)
            return
        notice(msg)

    # Tiers, highest precedence first; the inner list is how ties get expressed.
    # tcat's [order].statuses is a flat list and cannot express them — and means the
    # opposite anyway (it sorts `x` last for display; here `x` ranks highest for
    # dedup), which is why the two keys stay separate rather than being merged.
    tiers = merged.get('dedup', {}).get('priority', [])
    STATUS_PRIORITY = {c: len(tiers) - i for i, tier in enumerate(tiers) for c in tier}
    if not STATUS_PRIORITY:
        notice('no [dedup] priority in config — dedup will pick an arbitrary status '
               'when a task appears more than once')

    # Display rank, and the one place tdiff and tcat now agree on [order]. Rank is
    # position in the list, so `x` sorts last; see display_rank() for what an unlisted
    # status does. This is the *opposite* end from [dedup] above and stays a separate
    # key for that reason: a row that dedup picked `x` for still prints at the bottom.
    seq = merged.get('order', {}).get('statuses', [])
    DISPLAY_ORDER = {c: i for i, c in enumerate(seq)}
    if not DISPLAY_ORDER:
        notice('no [order] statuses in config — rows will sort by name alone')

    roles = merged.get('roles', {})
    PROJECT_STATUSES = set(roles.get('project', []))
    SETTLED_STATUSES = set(roles.get('settled', []))
    HIDDEN_STATUSES  = set(roles.get('hide', []))
    FULL_ROW         = set(roles.get('full_row', []))

    # [theme.*] and [roles] full_row are read here but painted only by tcat: in tdiff
    # the diff type owns the row colour, so a status colour would have nothing to
    # paint. The merged table is the superset and each tool reads what it uses.
    THEMES = {name: dict(table)
              for name, table in merged.get('theme', {}).items()
              if isinstance(table, dict)}

    if not PROJECT_STATUSES:
        notice('no [roles] project in config — project rows will not be excluded')

    # Headings never read, with '>' separating ancestors. Each component is normalised
    # on the way in, so a config may spell one "*Snapshot*", "Snapshot" or "snapshot"
    # indifferently. No notice for an empty list: fences already skip a frozen task
    # list, so naming a section is how a vault stops relying on that, not a thing every
    # vault must do.
    paths = []
    for raw in merged.get('exclude', {}).get('sections', []):
        parts = tuple(p for p in (_norm_heading(x) for x in str(raw).split('>')) if p)
        if parts:
            paths.append(parts)
    EXCLUDED_SECTIONS = tuple(paths)

    # Tags whose task lines are never read. Matched with a word boundary at the end, so
    # "#routine" does not also take "#routines" or "#routine/daily" with it — name those
    # separately if that is what the vault means. A tag is the other way a vault marks a
    # line as not-really-a-task, and naming them here is the same idea as naming a
    # section: the script has no opinion on which tags a vault uses.
    tags = []
    for raw in merged.get('exclude', {}).get('tags', []):
        t = str(raw).strip()
        if t:
            tags.append(re.compile(re.escape(t) + r'(?![\w/-])'))
    EXCLUDED_TAGS = tuple(tags)

    # How this vault writes each weekday marker, as a glob matched against the whole
    # lowercased line: "*|monday]]" finds `[[2026-08-24|monday]]` and a heading spelling
    # of it alike. The keys are the seven canonical day names because the code has to
    # order them — everything about how they *look* is the vault's to say. Without this
    # the weekly note has no ladder, so a date-derived week cannot bound it; the dailies
    # still truncate, since their bound is the filename.
    # One glob or a list of them: a vault that has changed how it writes a marker
    # keeps reading its own history by naming both spellings.
    days = merged.get('days', {})
    pats = []
    for d in WEEK_DAYS:
        raw = days.get(d) or ()
        globs = [raw] if isinstance(raw, str) else list(raw)
        for g in globs:
            g = str(g).strip().lower()
            if g:
                pats.append((d, re.compile(fnmatch.translate(g))))
    DAY_PATTERNS = tuple(pats)

    vault = merged.get('vault', {})
    DAILY_FOLDER  = vault.get('daily_folder', '')
    WEEKLY_FOLDER = vault.get('weekly_folder', '')

# tcat's sentinel, same value, so the two sort alike. Kept on its own line rather than
# trailing the assignment because tcat's check-core-sync.sh compares whole assignment
# lines, and a trailing comment would read as drift.
UNRANKED = 10 ** 6

def display_rank(ch):
    """Sort rank for a status char, from [order].statuses.

    A status the table does not list ranks last as a block — alphabetical within it,
    since the name is the next sort key — which is what the shipped statuses.toml
    promises and what tcat does with the same list. report_unlisted() names them.
    """
    return DISPLAY_ORDER.get(ch, UNRANKED)

def flush_notices():
    for m in NOTICES:
        print(f'{TOOL}: {m}', file=sys.stderr)

WIKILINK_RE = re.compile(r'\[\[([^\[\]]+)\]\]')

# A wikilink keeps its brackets and loses only its folder path, so `[[a/b|c]]` reads
# as `[[b|c]]`. This is what clean_text runs, and what tcat's tools/check-core-sync.sh
# lists in FUNCS.
def _strip_wiki_path(m):
    inner = m.group(1)
    if '|' in inner:
        target, alias = inner.split('|', 1)
        return f'[[{target.rsplit("/", 1)[-1]}|{alias}]]'
    return f'[[{inner.rsplit("/", 1)[-1]}]]'

def normalize_wikilinks(s):
    return WIKILINK_RE.sub(_strip_wiki_path, s)

_PUNCT = ',.;:!?-—…'

def _tokens(s):
    out = []
    for t in s.split():
        t = t.lower().strip(_PUNCT)
        if t:
            out.append(t)
    return out

TASK_RE = re.compile(r'^(\s*)- (\[.\]) (.+)$')

FENCE_RE = re.compile(r'^\s*(?:```|~~~)')

MDLINK_RE = re.compile(r'\[([^\[\]]+)\]\([^()]*\)')

# Any heading, at any level, however it is written — `## *Two Words*` and a plain
# `## Two Words` alike, since a config has to be able to name either. This replaced a
# pair of matchers that wanted a single italicised word and drove a section whitelist;
# sections are named in config now, so a heading's only remaining job is to bound a
# section and reset the day ladder.
HEAD_RE = re.compile(r'^(#{1,6})\s+(.+?)\s*$')

def _excluded(path, patterns):
    """Does the just-entered node at `path` open an excluded section? A pattern names
    an ancestor chain read outward-in ('plan > deferred'); intervening levels are
    allowed, so it survives a heading being added above or between."""
    for pat in patterns:
        if pat[-1] != path[-1]:
            continue
        it = iter(path[:-1])
        if all(p in it for p in pat[:-1]):
            return True
    return False

def _match_day(line, day_patterns):
    """Which weekday marker, if any, this line is. The glob is matched against the
    whole lowercased line, so a marker can be a heading, a bold word or a wikilink —
    the config says which, and nothing here has an opinion."""
    text = line.strip().lower()
    for day, pat in day_patterns:
        if pat.match(text):
            return day
    return None

def _norm_heading(s):
    """'## *Old Notes*' -> 'old notes'. Emphasis, backticks and case are formatting,
    not name: the config names a section the way a person would say it, and all of
    "*Old Notes*", "Old Notes" and "old notes" have to mean the same section."""
    s = s.replace('*', '').replace('_', '').replace('`', '')
    return ' '.join(s.split()).lower()

# The seven days, in week order. These are the *keys* of [days] in the config, never
# a claim about how a note writes them: the vault supplies a glob per day and this
# tuple only says what order they fall in. That order is the whole point — it answers
# "was this allocated to a day at or after the anchor", which is what stops a week
# derived from a date before that date.
WEEK_DAYS = ('sunday', 'monday', 'tuesday', 'wednesday',
             'thursday', 'friday', 'saturday')

def clean_text(s):
    """Un-escape Obsidian's bracket escapes, drop a trailing ' – ...' section suffix,
    then shorten wikilink paths and reduce markdown links to their display text.

    **The order is the whole point, and it diverges from tcat.** `strip_section_suffix`
    only skips a dash it can see is inside brackets, so it has to run while the
    brackets are still there. tcat reduces links first, which leaves
    `[[marc randolph on building netflix – tim ferriss (496)]]` looking like prose with
    a section suffix and truncates it at the dash — 18 of 455 names in this vault, some
    down to a third of their length. Un-escaping stays first either way, or `\\[\\[foo]]`
    never registers as bracket depth at all.

    Wikilinks keep their brackets (`normalize_wikilinks` only shortens the path), which
    is the second divergence: tdiff has always displayed `complete [[2026-W28]]`, and a
    name is a name whether or not the vault happened to link it. Markdown links do get
    reduced — that part is new, and without it a name carries a full URL.
    """
    s = s.replace('\\[', '[').replace('\\]', ']')
    s = strip_section_suffix(s)
    s = normalize_wikilinks(s)
    return MDLINK_RE.sub(lambda m: m.group(1), s)

def parse_note(lines, exclude_tags=(), exclude=(), skip_days=(), only_days=None,
               day_patterns=()):
    """Yield (indent, status_char, name, seq) for tasks in a note.

    exclude: heading paths ([exclude] sections) that are never read — a tuple of
    normalised components, outermost first, so ('archive',) names a heading anywhere
    and ('plan', 'deferred') names one under another. A section runs until the next
    heading at the same or shallower level, and the opening line is swallowed so an
    excluded section cannot set the day ladder either.

    Only headings bound a section. Bold text used to count as a node too, which read
    well until you asked where such a section *ends*: bold lines carry no level, so
    two in a row are siblings, one nested inside the other is indistinguishable from
    one following it, and a vault that bolds an ordinary paragraph grows a section by
    accident. A heading has a level and therefore an unambiguous end.

    A fenced block is not content: ``` / ~~~ toggle, and nothing inside is parsed —
    not tasks, not headings, not day markers. Fencing is one way a vault freezes a task
    list, and reading one back reports a day's frozen copy of the week as work of its
    own (one such pair of notes gave 89 rows where 19 were real). This parser used to
    ignore fences outright, which was safe only while the reader was `obsidian tasks` —
    that CLI never handed the fenced lines over.

    `exclude` is the filter for everything that is *not* fenced, and the durable one:
    a section is skipped because of what it is called, not because of how it happens to
    be formatted. No section name is known to this file.

    day_patterns: ((day, compiled_glob), ...) in week order — how this vault writes
    each weekday marker ([days] in the config). A line matching one opens that day and
    is not itself content; a heading ends whatever day was open, and may name a new one.
    Nothing here knows what a marker looks like, which is the point: `**monday**` and
    `[[2026-08-24|monday]]` are both just globs someone wrote down.

    skip_days: canonical day names whose tasks are dropped — tdiff's anchor bound, so
    that a task allocated to Thursday is not outstanding on Wednesday.

    only_days: the inverse, and the reason both exist. tcat asks a weekly note for one
    weekday's allocation; tdiff asks it for everything up to a day. Neither is a
    whitelist the module holds: both are the caller's, and a task under no marker is
    outside only_days, since it was allocated to no day at all.

    With no day_patterns nothing ever opens a day, so skip_days bounds nothing and
    only_days matches nothing; tdiff's dailies still truncate on their filenames.
    """
    cur_day = None
    outline = []        # (level, name) of every enclosing node, outermost first
    excl_depth = None   # depth of the excluded node we are inside, if any
    in_fence = False
    seq = 0
    for line in lines:
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = HEAD_RE.match(line)
        if m:
            lvl, title = len(m.group(1)), _norm_heading(m.group(2))
            while outline and outline[-1][0] >= lvl:
                outline.pop()
            outline.append((lvl, title))
            # A heading at or above the excluded one ends it — a sibling included.
            if excl_depth is not None and len(outline) <= excl_depth:
                excl_depth = None
            if excl_depth is None and _excluded([n for _, n in outline], exclude):
                excl_depth = len(outline)
            # A heading ends the open day, and may name one itself.
            cur_day = _match_day(line, day_patterns)
            continue
        if excl_depth is not None:
            continue
        day = _match_day(line, day_patterns)
        if day is not None:
            cur_day = day
            continue
        m = TASK_RE.match(line)
        if not m:
            continue
        if any(t.search(line) for t in exclude_tags):
            continue
        if cur_day in skip_days:
            continue
        if only_days is not None and cur_day not in only_days:
            continue
        seq += 1
        yield len(m.group(1)), m.group(2)[1], clean_text(m.group(3)), seq

class ObsidianError(Exception):
    """The obsidian CLI could not be run, or reported an error we can't interpret.
    `retry` is False for permanent failures (no binary), True for ones observed to be
    transient. `stalled` marks the timeout case, which is worth telling the user about
    because it's the only failure they feel as a pause."""
    def __init__(self, msg, retry=True, stalled=False):
        super().__init__(msg)
        self.retry = retry
        self.stalled = stalled

# A note that simply doesn't exist is normal (a day with no note, a week with no
# planning note) — obsidian reports it on stdout with exit status 0.
_MISSING_FILE_RE = re.compile(r'^Error: File "[^"]*" not found\.$')

def _env_int(name, default):
    try:
        return max(1, int(os.environ[name]))
    except (KeyError, ValueError):
        return default

# Concurrency is a small win (4 and 8 workers measure the same), so keep it low —
# the obsidian CLI talks to a single running app. TNOTES_WORKERS=1 forces serial reads.
# The prefix is the module's, not a tool's: these are read at import, before init()
# knows who is calling, and a reader that behaves differently per tool would be a way
# for the two to disagree about the vault.
FETCH_WORKERS = _env_int('TNOTES_WORKERS', 4)

# Roughly one obsidian call in a few hundred wedges and never returns, at the same
# rate whether we read serially or concurrently. The wedge is per-invocation — the app
# keeps answering everything else — so a short deadline plus a fresh call is the cure.
# A healthy call takes ~10ms (20ms worst of 320 measured), so 1s is ~100x headroom and
# keeps a stall inside the second this tool should always finish in. A merely slow call
# is not lost by a tight deadline: the retry catches it.
OBSIDIAN_TIMEOUT  = _env_int('TNOTES_TIMEOUT', 1)   # seconds per call

OBSIDIAN_ATTEMPTS = 3

SLOW_NOTICE_AFTER = 2.5     # seconds before telling the user we're waiting, not hung

DEBUG = bool(os.environ.get('TNOTES_DEBUG'))

_lines_cache = {}

def die(msg):
    """Report a fatal error and leave immediately. os._exit() because a wedged
    obsidian call can leave a pool thread blocked, and a normal exit would hang
    joining it."""
    sys.stdout.flush()
    sys.stderr.write(f"{TOOL}: {msg}\n")
    sys.stderr.flush()
    os._exit(2)

def _on_uncaught(exc_type, exc, tb):
    """Ctrl-C should leave at once — without a traceback, and without waiting on
    reader threads that may be stuck on a slow obsidian call."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.stdout.flush()
        sys.stderr.write(f'\n{TOOL}: interrupted\n')
        sys.stderr.flush()
        os._exit(130)
    sys.__excepthook__(exc_type, exc, tb)

sys.excepthook = _on_uncaught

def _run_obsidian(file_path):
    """Return the raw markdown lines of one vault file, retrying a failure that might
    be transient. Raises ObsidianError once the attempts are spent."""
    for attempt in range(1, OBSIDIAN_ATTEMPTS + 1):
        try:
            return _obsidian_once(file_path)
        except ObsidianError as e:
            if not e.retry or attempt == OBSIDIAN_ATTEMPTS:
                raise
            if e.stalled:
                sys.stderr.write(f"{TOOL}: obsidian stalled on {file_path}, retrying\n")

def _obsidian_once(file_path):
    """One `obsidian read` call. Raises ObsidianError if the CLI is missing or its
    output isn't something we can trust.

    `read` rather than `tasks`: the flat task list `tasks` returns has already thrown
    away the headings an exclude list names, and the day markers that say when a
    planned task was due. tcat has
    always read raw markdown for that reason; this is the seam where the two tools
    stopped disagreeing about what a note contains."""
    argv = ['obsidian', 'read', f'file={file_path}']
    started = time.monotonic()
    try:
        # stdin is closed: this is never an interactive call, and a CLI that decided
        # to read from the terminal would block every reader thread behind it.
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=OBSIDIAN_TIMEOUT)
    except FileNotFoundError:
        raise ObsidianError(
            f"the 'obsidian' CLI was not found on PATH — {TOOL} reads the vault through it",
            retry=False,
        )
    except subprocess.TimeoutExpired:
        raise ObsidianError(
            f"'obsidian read file={file_path}' produced nothing within "
            f"{OBSIDIAN_TIMEOUT}s on {OBSIDIAN_ATTEMPTS} attempts — is Obsidian running "
            f"with the vault open? (TNOTES_TIMEOUT=N raises the limit)",
            stalled=True,
        )
    except OSError as e:
        raise ObsidianError(f"could not run 'obsidian': {e}")

    if DEBUG:
        sys.stderr.write(f"{TOOL}: {time.monotonic() - started:6.3f}s  {file_path}\n")

    out, err = result.stdout, result.stderr.strip()
    if result.returncode != 0:
        detail = err or out.strip()
        first = detail.splitlines()[0] if detail else '(no output)'
        raise ObsidianError(f"'obsidian read file={file_path}' exited {result.returncode}: {first}")

    lines = out.splitlines()
    if not lines:
        # An empty note is a legitimately empty side, the same as a missing one.
        return []
    # Only the *first* line can be an error report. Scanning them all was safe while
    # this read a task list; on raw markdown a note that happens to open a line with
    # "Error:" would be misread as a failed read.
    first = lines[0].strip()
    if _MISSING_FILE_RE.match(first):
        return []
    if first.startswith('Error:'):
        raise ObsidianError(f"obsidian could not read {file_path!r}: {first}")
    if not out.strip() and err:
        raise ObsidianError(f"obsidian returned nothing for {file_path!r}: {err.splitlines()[0]}")
    return lines

def obsidian_lines(file_path):
    """Memoized markdown lines for one vault file."""
    if file_path not in _lines_cache:
        try:
            _lines_cache[file_path] = _run_obsidian(file_path)
        except ObsidianError as e:
            die(str(e))
    return _lines_cache[file_path]

def prefetch(paths):
    """Warm the line cache for several vault files at once. Each obsidian call is
    mostly waiting on the app, so overlapping them is a win for week modes.

    `read` returns a whole note where `tasks` returned a handful of lines, so each
    payload is larger — but the call count is unchanged, and the wait was always
    the round trip rather than the bytes."""
    todo = [p for p in dict.fromkeys(paths) if p not in _lines_cache]
    if not todo:
        return
    # Named slow_timer, not notice: notice() is the deferred-message function, and a
    # local of that name here would shadow it for the whole body.
    slow_timer = threading.Timer(SLOW_NOTICE_AFTER, sys.stderr.write,
                                 (f"{TOOL}: waiting on obsidian ({len(todo)} files)…\n",))
    slow_timer.daemon = True
    slow_timer.start()
    try:
        if FETCH_WORKERS < 2 or len(todo) < 2:
            for p in todo:
                obsidian_lines(p)
            return
        # Both branches go through obsidian_lines() so the vault read has exactly one
        # seam. Submitting _run_obsidian directly used to leave the memo wrapper
        # bypassed on the concurrent path — a second entry point that anything hooking
        # the read would silently miss.
        with ThreadPoolExecutor(max_workers=min(FETCH_WORKERS, len(todo))) as ex:
            for p in todo:
                ex.submit(obsidian_lines, p)
    finally:
        slow_timer.cancel()

def daily_path(d):
    return f'{DAILY_FOLDER}/{d}' if DAILY_FOLDER else d

def weekly_path(week_label):
    return f'{WEEKLY_FOLDER}/{week_label}' if WEEKLY_FOLDER else week_label

def cluster_records(records):
    """Cluster records that name the same logical task, using union-find. Two bases
    are the same task when their token sets are equal, or when one is a strict subset
    of the other and both start with the same token. Returns a list of clusters (each
    a list of records) in first-seen order of their root base."""
    bases_in_order, seen = [], set()
    for base, _, _ in records:
        if base not in seen:
            seen.add(base)
            bases_in_order.append(base)

    parent = {b: b for b in bases_in_order}
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Bucketing on the two merge keys gives the same clusters as an all-pairs scan
    # without tokenizing (or comparing) every pair. Tokenless bases could only merge
    # by exact string equality, which distinct bases never satisfy — so skip them.
    by_tokens, by_first = {}, {}
    for b in bases_in_order:
        toks = _tokens(b)
        if not toks:
            continue
        key = frozenset(toks)
        by_tokens.setdefault(key, []).append(b)
        by_first.setdefault(toks[0], []).append((b, key))

    for group in by_tokens.values():
        for b in group[1:]:
            union(group[0], b)
    for group in by_first.values():
        for i, (a, sa) in enumerate(group):
            for b, sb in group[i + 1:]:
                if sa < sb or sb < sa:
                    union(a, b)

    by_root = {}
    for rec in records:
        by_root.setdefault(find(rec[0]), []).append(rec)

    clusters, emitted = [], set()
    for b in bases_in_order:
        r = find(b)
        if r in emitted:
            continue
        emitted.add(r)
        clusters.append(by_root[r])
    return clusters

def status_char(s):
    """The bare status char, whether the caller stores it as 'x' or as '[x]'.

    tdiff brackets, tcat does not. That is a rendering choice each tool made and
    neither is wrong; what would be wrong is a shared dedup holding an opinion about
    it, so this reads both forms instead of forcing one."""
    return s[1] if len(s) == 3 and s[0] == '[' and s[2] == ']' else s

def materialize(clusters):
    """Reduce each cluster to one (canonical_base, winning_status) pair.

    Both tools reduce by [dedup] priority now. tcat used to take the *last occurrence
    in page order* — the reasonable-sounding rule that the latest statement wins — and
    that is exactly the disagreement the vendored check could not see, since it never
    covered this function: one vault, two tools, two statuses for the same task.
    Priority is the better rule anyway, because "done" is the truest thing you can say
    about a task also written [/] on Tuesday, whichever line came last. Page order
    survives as the tie-break."""
    tasks, order = {}, []
    for members in clusters:
        # Canonical: most recent day; tie-break by longest base.
        canonical = max(members, key=lambda r: (r[2], len(r[0])))[0]
        # Winning status: highest STATUS_PRIORITY; tie-break by latest day. The tuple
        # ran (day, priority) until now, which quietly made it the other way round —
        # the last note to mention a task won outright and [dedup] only settled a
        # same-day tie, so a Friday [/] beat a Tuesday [x].
        winning = max(members, key=lambda r: (STATUS_PRIORITY.get(status_char(r[1]), 0), r[2]))[1]
        order.append(canonical)
        tasks[canonical] = winning
    return tasks, order

def week_span(d):
    """Return (sunday, saturday, label) for the US-format Sun-Sat week containing d."""
    if isinstance(d, str):
        d = date.fromisoformat(d)
    days_back = (d.weekday() + 1) % 7   # Mon=0..Sun=6 -> Sun=0
    sun = d - timedelta(days=days_back)
    sat = sun + timedelta(days=6)
    # The label's year is the *Saturday's*, not the Sunday's. Week 1 is the week
    # containing Jan 1, so a week straddling New Year belongs to the year it ends
    # in — which is what the vault's moment `gggg[-W]ww` filenames say. Anchoring
    # on the Sunday called Sun 2026-12-27 → Sat 2027-01-02 `2026-W53`, a file that
    # does not exist, and made the vault's real `2027-W01` unreachable by name.
    # Only the straddling week per year is affected; all others are unchanged.
    jan1 = date(sat.year, 1, 1)
    days_back = 0 if jan1.isoweekday() == 7 else jan1.isoweekday()
    week_num = ((sun - jan1).days + days_back) // 7 + 1
    return sun, sat, f"{sat.year}-W{week_num:02d}"

def restore(s):
    if s.startswith('__NEG__'):
        return '-' + s[7:]
    return s

_ISO_RE = re.compile(r'\d{4}-\d{2}-\d{2}')

_OFF_RE = re.compile(r'([-+])(\d+)')

# One line each so tools/check-core-sync.sh, which compares constants by their
# assignment line, actually covers them. Both the full name and its 3-letter prefix
# map to the same index.
_WEEKDAY_NAMES = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']

WEEKDAYS = {n[:k]: i for i, n in enumerate(_WEEKDAY_NAMES) for k in (3, len(n))}

_WEEK_REL_RE   = re.compile(r'[wW]([-+]\d+|0)$')

_WEEK_SHORT_RE = re.compile(r'[wW](\d{1,2})$')

_WEEK_FULL_RE  = re.compile(r'[wW](\d{4}-W\d{2})$')

def resolve_date(s):
    today = date.today()
    if s in ('today', '0'):
        return today.isoformat()
    if s == 'yesterday':
        return (today - timedelta(days=1)).isoformat()
    if s == 'tomorrow':
        return (today + timedelta(days=1)).isoformat()
    low = s.lower()
    if low in WEEKDAYS:
        # Backwards: the most recent occurrence at or before today. A future day
        # has no tasks yet, so resolving forwards would always come back empty.
        days_back = (today.weekday() - WEEKDAYS[low]) % 7
        return (today - timedelta(days=days_back)).isoformat()
    m = _OFF_RE.fullmatch(s)
    if m:
        n = int(m.group(2))
        if n < 1:
            raise DateError(f"invalid date: {s!r} (use 0 for today)")
        delta = -n if m.group(1) == '-' else n
        return (today + timedelta(days=delta)).isoformat()
    if _ISO_RE.fullmatch(s):
        try:
            return date.fromisoformat(s).isoformat()
        except ValueError:
            raise DateError(f"invalid date: {s!r}")
    raise DateError(
        f"invalid date: {s!r} (expected YYYY-MM-DD, 'today', 'yesterday', "
        f"'tomorrow', '0', -N/+N, a weekday name, or w##)"
    )

def resolve_week_label(s):
    """Return 'YYYY-W##' if s is a w## specifier, else None.

    Three spellings, tried in this order. `w2026-W30` names a week outright. `w-1` / `w0`
    / `w+1` are relative to the week containing today, and are what replaced the offset
    flags both tools used to carry. `w30` is a bare week number in the current year.

    The relative form has to be tried before the bare number, or `w0` would read as week
    zero — a week no calendar has, and one that used to resolve to a phantom label.
    """
    m = _WEEK_FULL_RE.fullmatch(s)
    if m:
        return m.group(1)
    m = _WEEK_REL_RE.fullmatch(s)
    if m:
        return week_span(date.today() + timedelta(weeks=int(m.group(1))))[2]
    m = _WEEK_SHORT_RE.fullmatch(s)
    if m:
        return f'{date.today().year}-W{int(m.group(1)):02d}'
    return None

def _week_label_to_sunday(week_label):
    """Convert 'YYYY-W##' (US convention: week 1 contains Jan 1, weeks start Sunday)."""
    year, week_num = int(week_label[:4]), int(week_label[6:])
    jan1 = date(year, 1, 1)
    days_back = 0 if jan1.isoweekday() == 7 else jan1.isoweekday()
    return jan1 - timedelta(days=days_back) + timedelta(weeks=week_num - 1)

def report_unlisted(seen):
    """Name status chars this run met that the config does not rank.

    Two tables rank a status and neither is loud about a gap. Missing from [dedup] it
    ranks 0 and loses every tie silently — invisible until a task wears it twice.
    Missing from [order] it sorts last, which just looks like a choice. Both are
    reported, and separately, because a config can easily have one and not the other.
    tcat says the same thing about [order] in its own report_unlisted().
    """
    # Project statuses are excluded from every output, so they never reach a dedup
    # tie-break and want no rank. Reporting them would be noise on every run.
    seen = seen - PROJECT_STATUSES
    unlisted = sorted(seen - set(STATUS_PRIORITY))
    if unlisted and STATUS_PRIORITY:
        chars = ' '.join(f'[{c}]' for c in unlisted)
        notice(f'not in [dedup] priority, ranked 0: {chars}')
    unranked = sorted(seen - set(DISPLAY_ORDER))
    if unranked and DISPLAY_ORDER:
        chars = ' '.join(f'[{c}]' for c in unranked)
        notice(f'not in [order] statuses, so sorted last: {chars}')
