# minutely

Record a meeting, transcribe it on your own machine, and get minutes with owned
action points.

It runs on one machine. There is no service to sign up for, nothing is uploaded
anywhere, and there are **zero runtime dependencies** — recording is the
browser's own `MediaRecorder`, the UI is served by `http.server`, storage is
`sqlite3`, and the default minutes engine is plain Python. `pip install -e .`
pulls nothing.

```
minutely demo                 # minute a bundled sample meeting, no setup at all
minutely record               # open the recorder in your browser
minutely notes --edit         # the notes you typed; the engines build on them
minutely upcoming             # what's next on your calendar
minutely teams pull           # pull Teams meeting transcripts and minute them
minutely share --to sam@x.com # email the minutes
minutely import meeting.vtt   # already have a transcript? start from that
minutely minutes              # write the minutes for the latest meeting
minutely actions              # the action register, across every meeting
minutely done 4               # tick one off
```

---

## Quick start

```bash
cd meeting-minutes
pip install -e .

minutely demo
```

`demo` needs no microphone, no network, and no speech recognition: it minutes a
bundled transcript of an invented product meeting, so you can see the output
shape before deciding whether the rest is worth setting up.

To record something real:

```bash
minutely record          # opens http://127.0.0.1:<port>/ in your browser
```

Give the meeting a title, press **Start recording**, and the page streams audio
to disk every five seconds. Press **Stop and save** and — if a whisper binary is
installed — it transcribes and writes the minutes on its own.

## Type notes; get minutes

The best signal in any meeting is what the person in the room decided was worth
writing down. So the recorder page is, first, a notepad — you jot as it happens,
and afterwards the engine builds the minutes *around* your notes rather than
instead of them.

```
Pricing                              →   your headings become the sections
- 49 -> 65, bundle analytics         →   your points are kept, verbatim
TODO: Sam: circulate the mechanics   →   your actions are actions, owned by Sam
[ ] check the grandfather rule       →   and this one stays unassigned
```

Four conventions, all ones people already use without being asked:

| You type | It becomes |
|---|---|
| a short line on its own | a section heading, filled in from the transcript |
| `- something` | a point under that heading, kept word for word |
| `TODO:` / `[ ]` / `x -> Name` | an action point |
| `Name: do the thing` | an action owned by that person |

Your notes are never rewritten and never dropped — they appear verbatim at the
top of the minutes under **Your notes**. Everything else is built underneath
them. Writing something down also promotes it: a commitment the engine was
unsure about becomes a real action once you have independently noted it.

Notes are saved as you type, so a crash costs nothing. From the terminal:

```bash
minutely notes                       # show them
minutely notes --edit                # open them in $EDITOR
minutely notes --file standup.md     # or bring them from a file
```

### Templates

A stand-up and a client call want different documents. Templates set which
sections appear, in what order, and what they are called:

```bash
minutely templates
minutely minutes --template client
```

```
default    General meeting    Summary, discussion, decisions, actions.
standup    Stand-up           Short, per-person, blockers to the front.
1-1        One-to-one         Discussion, agreements, follow-ups.
client     Client call        What they asked for, what we committed to.
```

A client call's minutes say **Commitments** rather than "Action points" and
**Agreed with the client** rather than "Decisions" — because those are the words
that matter when the notes get quoted back at you. The picker sits under the
notepad in the UI.

## What comes out

```
ACTION POINTS
 1. Spin up a Keycloak instance and run the integration suite against it [01:14]
    [Priya / due 2026-08-27]
 2. Write that up as a one-pager for the board [03:52]
    [Sam / due 2026-09-01]
 ...
```

Every action carries the timestamp it came from, so anyone who disputes it can
go and listen. Minutes render as Markdown (for a wiki or a ticket), a printable
HTML page, plain text, or JSON:

```bash
minutely show --format md
minutely export --format html -o minutes.html
```

The three sections that matter are **decisions** (what was concluded),
**action points** (who does what, by when), and **possible actions** — things
the engine spotted but is not confident anybody committed to. That last section
exists because minutes that list work nobody agreed to are worse than minutes
that miss a line; the uncertain items are visible, but they stay out of the
action register until a human promotes them.

## Your calendar, and one click to record

Once Microsoft 365 is connected (below), `minutely record` opens with an **Up
next** list: the meetings on your calendar for the next few hours, each with a
**Record** button. Clicking one starts recording immediately with the meeting's
title, its attendees, and — for an online meeting — tab audio already switched
on. Nothing to type while people are waiting for you.

```bash
minutely upcoming            # the same list, in the terminal
minutely upcoming --hours 3
```

```
WHEN     IN        WHERE      MEETING
09:30    12 min    Teams      Weekly product sync
11:00    1 h 42    in person  Design review — Room 3
14:00    4 h 42    Teams      Board prep [recorded]
```

A recording started this way remembers which calendar event it was, which does
two useful things: `minutely teams pull` later recognises the same meeting
instead of importing a second copy of it, and the attendees' addresses come
along so sharing the minutes is one click rather than a round of retyping.

## Recording

Recording happens in the browser because that is where the microphone
permission already lives, and where every platform has a working audio stack —
no PortAudio, no device enumeration, no third-party wheel between you and the
room.

- **The room.** The default is your microphone, which is what you want for a
  meeting you are sitting in.
- **A call in a browser tab.** Tick *Also capture the meeting tab's audio* and
  pick the Zoom / Teams / Meet tab. The mic and the tab are mixed into one
  track, so remote participants are recorded too. This relies on tab audio
  capture, which Chrome supports and most other browsers do not; if it is
  unavailable the app says so and records the microphone alone.
- **Teams.** For Teams specifically, prefer the integration below: Teams has
  already recorded and transcribed the meeting, with speaker names, and pulling
  that beats re-recording it. Local capture is the fallback for meetings nobody
  transcribed.
- **Crash safety.** Audio is appended to the file every five seconds. A crash
  costs seconds, not the meeting.

> **Consent.** Tell everyone in the room that you are recording. In many
> jurisdictions recording a conversation without the other parties' consent is
> a criminal offence. The app reminds you; it cannot make the call for you.

## Transcription

`minutely` does not implement speech recognition. It drives whichever whisper
build you already trust:

```bash
# whisper.cpp
minutely config --whisper-bin whisper-cli --whisper-model ~/models/ggml-base.en.bin

# openai-whisper (or the faster-whisper CLI, same flags)
minutely config --whisper-bin whisper --whisper-model small.en
```

whisper.cpp reads 16 kHz mono WAV only, so browser audio is converted with
`ffmpeg` first; the OpenAI CLI reads the recording directly and needs no
`ffmpeg`. Either way nothing leaves the machine.

If you would rather not run speech recognition at all, bring your own
transcript — Teams, Zoom, and Meet all export one:

```bash
minutely import ~/Downloads/standup.vtt --title "Monday standup" --process
minutely config --transcriber none      # stop asking for a whisper binary
```

`.vtt`, `.srt`, whisper `.json` (both the OpenAI and whisper.cpp layouts), and
plain text with `Name:` labels are all understood.

## Microsoft Teams

Teams records and transcribes meetings itself, server-side, and its transcript
carries the one thing local recording cannot give you: **who said what**. So
this integration does not re-record your Teams calls. It signs in as you, finds
your Teams meetings, and pulls the transcript Teams already produced — then
runs it through the same engines as everything else.

```bash
minutely teams login          # one-time device-code sign-in
minutely teams list           # Teams meetings on your calendar
minutely teams pull           # import the last 7 days and minute them
minutely teams pull --days 30 --match "product sync"
```

Because the transcript arrives with speaker labels, actions come out owned:

```
+ 2026-08-24 Weekly product sync
= 2026-08-25 Design review — already imported
. 2026-08-25 Quick sync — Teams captured no transcript for this meeting

1 meeting(s) minuted. Read them with: minutely show
```

Pulling twice is safe: meetings are tracked by their calendar event id, so the
second run imports nothing. `--force` re-imports one anyway.

The same thing is in the browser UI — `minutely record` shows a **Microsoft
Teams** panel with sign-in and a *Pull recent Teams meetings* button.

### What it cannot do

**It cannot press Record for you.** Nothing in Microsoft Graph starts a Teams
recording; a participant does that in the Teams client, or an administrator
sets a compliance-recording policy for the tenant. If nobody turned on
recording or transcription while the meeting was running, there is nothing to
pull — capture the audio locally instead (`minutely record`).

It also reads only as far as you can: your meetings, your tenant's rules.

### One-time setup

You need a Microsoft Entra app registration. It takes about three minutes and
issues no secret — this is a public client, so there is nothing to keep safe.

1. Go to <https://entra.microsoft.com> → **App registrations** → **New
   registration**. Name it anything; leave the redirect URI blank.
2. **Authentication** → **Advanced settings** → **Allow public client flows** →
   **Yes**. Device code sign-in does not work without this.
3. **API permissions** → **Add a permission** → **Microsoft Graph** →
   **Delegated permissions**, and add:

   | Permission | Why |
   |---|---|
   | `Calendars.Read` | find your Teams meetings and their join URLs |
   | `OnlineMeetings.Read` | turn a join URL into the meeting behind it |
   | `OnlineMeetingTranscript.Read.All` | read the transcripts of those meetings |
   | `OnlineMeetingRecording.Read.All` | *optional* — only for `--with-recording` |
   | `Mail.Send` | *optional* — only for `minutely share` from your mailbox |
   | `User.Read` | show which account is signed in |

   Add `Mail.Send` too if you want minutely to email the minutes from your
   own mailbox (`minutely teams login --with-email`).

4. Click **Grant admin consent**. The transcript and recording scopes require
   it; if you are not an administrator, someone who is will have to approve
   them once for the tenant.
5. Copy the **Application (client) ID** and hand it to minutely:

```bash
minutely config --teams-client-id 11111111-2222-3333-4444-555555555555
minutely teams login
```

`teams login` prints a short code and a URL. Type the code into
microsoft.com/devicelogin in a browser where you are already signed in to work,
and the terminal picks up from there. Tokens land in
`~/.local/share/minutely/teams-token.json` at `0600`; the refresh token rotates
on every use and is written to disk before it is used. `minutely teams logout`
forgets them locally (it does not revoke consent — do that in Entra).

Recordings are **not** requested by default: reading a transcript needs no
access to anybody's video. Opt in with `minutely teams login --with-recordings`
and then `minutely teams pull --with-recording`, and be aware an hour of Teams
video is a few hundred megabytes.

### When it does not work

Microsoft's answers are specific, so minutely passes them through rather than
flattening everything to "failed":

| What you see | What it means |
|---|---|
| *Teams captured no transcript for this meeting* | Nobody turned on recording or transcription. Nothing to fix — record locally next time. |
| *your Microsoft 365 administrator has turned off Graph API access to Teams transcripts* | A tenant switch, not a permission. An admin re-enables it in the Teams admin centre. |
| *the sign-in is missing consent for this permission* | The transcript scope was never admin-consented. Step 4 above. |
| *no speaker attribution (tenant policy)* | The tenant forbids speaker-attributed transcripts. The text still imports; actions come out unassigned. |
| *no Teams meeting behind that invite* | The calendar entry is not a Teams meeting Graph can resolve — for instance a channel meeting created outside the calendar. |

Two Graph limits are worth knowing up front: the transcript APIs only cover
meetings that have a calendar event behind them, and they only work while the
meeting has not expired from Microsoft's retention window. Pull regularly
rather than going back a year.

### The Teams desktop app, without a transcript

If the meeting was never transcribed and you are in the desktop client (so
there is no tab to capture), record the system audio and import the file:

```bash
# macOS (needs a loopback device such as BlackHole)
ffmpeg -f avfoundation -i ":BlackHole 2ch" meeting.wav
# Linux (PulseAudio / PipeWire monitor source)
ffmpeg -f pulse -i default.monitor meeting.wav
# Windows
ffmpeg -f dshow -i audio="Stereo Mix (Realtek Audio)" meeting.wav

minutely import meeting.wav --title "Design review" --process
```

That gives you one unlabelled voice track — the same trade-off as any
in-the-room recording.

## The two minutes engines

| | `rules` (default) | `claude` |
|---|---|---|
| Where it runs | your machine | the Anthropic API |
| Network | none | yes — the transcript is sent |
| Extra install | none | `pip install "minutely[llm]"` |
| Determinism | identical output every run | varies |
| Prose quality | trimmed quotes | paraphrased and readable |

```bash
minutely minutes --engine claude     # per run
minutely config --engine claude      # or make it the default
export ANTHROPIC_API_KEY=...
```

Both engines take your typed notes as the spine of the document.

**`rules`** matches the sentence shapes people actually use to commit to work —
*"I'll do X"*, *"Sam, can you do X"*, *"we need X by Friday"* — attributes each
one to a speaker, resolves spoken deadlines ("by next Tuesday", "end of the
month") against the meeting date, and refuses the near-misses: past tense,
hypotheticals, questions, and pleasantries. It cannot write good prose, and it
never invents an action.

**`claude`** produces materially better summaries and topic titles. It is kept
honest by two things: the response is constrained to a JSON schema, so it is
parsed rather than scraped, and every action and decision must come back with a
verbatim quote which is then resolved against the local transcript. A quote
that matches nothing gets no citation, and the item is held back for review
rather than published as fact.

## The screen

```
┌──────────────┬────────────────────────────────────────────┐
│ Up next      │  Weekly product sync                       │
│  09:30 Sync ▸│  2026-08-24 · 42 min · Dana, Priya, Sam     │
│  11:00 1:1  ▸│  ▶ ──────────────────────────── 42:00      │
│              │                                            │
│ Meetings     │  YOUR NOTES                                │
│  Today       │  ┌──────────────────────────────────────┐  │
│   Weekly ●   │  │ Pricing                              │  │
│  Yesterday   │  │ - 49 -> 65, bundle analytics         │  │
│   Design rev │  └──────────────────────────────────────┘  │
│              │  [Enhance notes] [Stand-up ▾] [Copy]       │
│ Teams        │                                            │
│  [Pull]      │  SUMMARY / DISCUSSION / DECISIONS /        │
│              │  ACTION POINTS / TRANSCRIPT / EMAIL        │
└──────────────┴────────────────────────────────────────────┘
```

The rail on the left is the calendar on top and everything you have recorded
underneath, grouped by day. The pane on the right is one document per meeting:
the player, your notes, and the minutes built from them — with the transcript
searchable in a panel at the bottom and the share box under that.

## Listening back

Recordings are kept, and every meeting page has a player. The citations in the
minutes are the interesting part: each `[01:14]` is a button, and clicking it
plays the recording from two seconds before the line that produced the action.
When someone disputes an action point, that is the whole argument settled.

Audio is served from the local server with range requests, so scrubbing a
two-hour meeting does not load two hours of audio.

```bash
minutely list                          # everything recorded, newest first
minutely show 2026-08-24-0930-7f3a     # the minutes for one of them
minutely delete <id> --yes --files     # and this removes it for good
```

## Sending the minutes

```bash
minutely share                                  # to the meeting's attendees
minutely share --to sam@example.com --note "Sorry I missed this one."
minutely share --with-transcript --dry-run      # see exactly what would go
```

The minutes go as a real email — HTML body, plain-text alternative, and the
markdown attached — from one of two places:

| | Microsoft 365 | SMTP |
|---|---|---|
| Setup | `minutely teams login --with-email` | `minutely config --smtp-host … --smtp-from …` |
| Sends from | your real mailbox, saved to Sent Items | whatever the server allows |
| Password | none — it uses the sign-in you already have | `MINUTELY_SMTP_PASSWORD`, read at send time, never stored |

Recipients default to the attendees on the calendar invite. There is a **Send by
email** box on every meeting page in the UI, pre-filled the same way.

The recording itself is never attached. It is large, and the people in it agreed
to be minuted, not forwarded — if someone needs the audio, they can be sent the
file deliberately.

## The action register

Actions outlive the meeting they came from:

```bash
minutely actions                    # everything still open, soonest first
minutely actions --owner priya
minutely actions --status all
minutely done 4                     # or: minutely reopen 4 / minutely dropped 4
```

Regenerating minutes — with a better engine, or after fixing a transcript —
syncs the register rather than replacing it. An action you ticked off stays
ticked off.

## Where your data lives

Everything is under one directory, so backing it up or destroying it is one
command:

```
~/.local/share/minutely/          # $XDG_DATA_HOME, or %LOCALAPPDATA%\minutely
├── recordings/                   # the audio, exactly as the browser produced it
├── transcripts/                  # .vtt per meeting
├── exports/
├── minutely.sqlite3              # meetings, minutes, action register
├── teams-token.json              # Microsoft refresh token, 0600
└── config.json
```

Override with `MINUTELY_HOME`. The tree is created `0700`. Nothing is written
into the repository, and no API key is stored on disk — the Claude engine reads
`ANTHROPIC_API_KEY` from the environment.

```bash
minutely delete <meeting-id> --yes --files   # remove a meeting and its audio
```

## The local server

`minutely record` binds to `127.0.0.1` only, and defends itself three ways,
because "it's only localhost" is not a security model — every process on the
machine can reach loopback, and so can any website you happen to be visiting:

- **Loopback bind** — the socket is never on `0.0.0.0`.
- **Session token** — minted per run, injected into the page, required on every
  API request. A page you happen to be browsing cannot guess it.
- **Host header check** — requests whose `Host` is not a loopback literal are
  rejected, which is what stops DNS rebinding.

The page loads nothing from the network and is served under a
`default-src 'none'` content security policy.

## Command reference

| Command | What it does |
|---|---|
| `minutely record` / `ui` | open the browser recorder |
| `minutely import <file>` | bring in audio or an existing transcript (`--process` to do the lot) |
| `minutely transcribe [meeting]` | run speech recognition over a meeting's audio |
| `minutely minutes [meeting]` | write minutes (`--engine`, `--format`) |
| `minutely show [meeting]` | print the minutes already generated |
| `minutely export [meeting]` | write them to a file (`--format md/html/txt/json`) |
| `minutely list` | every meeting |
| `minutely notes` | show or replace the notes you typed (`--set`, `--file`, `--edit`, `--clear`) |
| `minutely templates` | the available minutes templates |
| `minutely upcoming` | what is next on your calendar (`--hours`) |
| `minutely share` | email the minutes (`--to`, `--note`, `--with-transcript`, `--dry-run`) |
| `minutely actions` | the action register (`--status`, `--owner`, `--meeting`) |
| `minutely done/reopen/dropped <id>` | change an action's status |
| `minutely teams login/status/logout` | Microsoft 365 sign-in for the Teams integration |
| `minutely teams list` | Teams meetings on your calendar |
| `minutely teams pull` | import Teams transcripts and minute them (`--days`, `--match`, `--with-recording`) |
| `minutely config` | show or change settings |
| `minutely demo` | minute the bundled sample meeting |
| `minutely delete <meeting>` | remove a meeting (`--files` for the audio too) |

Omitting the meeting argument means "the most recent one". Every command takes
`--json`.

## Architecture

```
browser MediaRecorder ──chunks──> server.py ──> recordings/*.webm
                                                     │
                                          transcribers/whisper.py
                                                     │
Microsoft Graph ──teams/sync.py──────────────> transcripts/*.vtt
 (transcript Teams already made)                     │
                              engines/rules.py  or  engines/claude.py
                                                     │
                                       store.py (sqlite) ──> render.py
```

| Module | Responsibility |
|---|---|
| `pipeline.py` | the three verbs — import, transcribe, minute — shared by the CLI and the UI so they cannot drift |
| `transcripts.py` | VTT / SRT / whisper JSON / plain text in, `Transcript` out |
| `engines/rules.py` | the offline extraction: notes, actions, owners, deadlines, decisions, topics |
| `templates.py` | which sections a stand-up, a one-to-one, or a client call gets |
| `engines/claude.py` | the Anthropic engine, plus the quote-grounding that keeps it honest |
| `teams/` | Microsoft 365 sign-in (`auth.py`), a small Graph client (`graph.py`), the calendar (`calendar.py`), and Teams-to-minutes (`sync.py`) |
| `share.py` | composing the email, and sending it via Microsoft 365 or SMTP |
| `store.py` | meetings, transcripts, minutes history, and the action register |
| `render.py` | Markdown, HTML, text, JSON |
| `server.py` + `ui/app.html` | the loopback recorder and review UI |

## Development

```bash
pip install -e ".[dev]"
ruff check src tests
mypy src
pytest -q
```

The test suite runs against a throwaway `MINUTELY_HOME`, needs no audio, no
network, no whisper binary, and no Microsoft tenant. It exercises the HTTP
server over real requests, and the whole Teams path — device-code sign-in,
token refresh, throttling, paging, transcript fallback, deduplication — against
a fake Microsoft (`tests/fakes.py`).

The browser UI has its own tests, which drive the real page in Chromium and
skip themselves when Playwright is not installed:

```bash
pip install -e ".[dev,ui-tests]" && playwright install chromium
pytest tests/test_ui_browser.py -q
```

## Limitations

- **No speaker diarisation in local recordings.** whisper transcribes words, not
  who said them. A recording of a room gives one unlabelled voice, so actions
  from it come out unassigned. Transcripts that already carry speaker labels —
  `minutely teams pull`, a Zoom or Meet export, or a `Name:` text file — keep
  them, and that is where owner attribution comes from.
- **minutely cannot start a Teams recording.** No API can. Someone has to press
  Record in the meeting, or the tenant has to have a recording policy.
- **English.** The rules engine's patterns are English. `--language` is passed
  to whisper, but a French transcript will be transcribed well and minuted
  badly. Use `--engine claude` for other languages.
- **It is a draft.** Every renderer says so at the bottom. Read the minutes
  before you send them.
- **The calendar is Microsoft 365 only.** `calendarView` expands recurring
  meetings server-side, which is why it is the source; there is no Google
  Calendar or `.ics` support yet.

## Licence

MIT.
