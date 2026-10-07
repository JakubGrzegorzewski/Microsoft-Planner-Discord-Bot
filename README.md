# Planner Discord bot

Create Microsoft Planner tasks from Discord and get a message in a Discord channel when a
task is completed. Built on discord.py 2.x (slash commands) and the Microsoft Graph API.

- [Commands](#commands)
- [How it works](#how-it-works)
- [Setup](#setup): [Discord](#1-discord-application) · [Entra app](#2-entra-app-registration) · [Planner IDs](#3-planner-ids) · [.env](#4-configuration) · [check](#5-check-the-microsoft-365-side)
- [Run with Docker](#run-with-docker) · [Run locally](#run-locally) · [systemd](#run-with-systemd)
- [Test checklist](#test-checklist)
- [Configuration reference](#configuration-reference)
- [Troubleshooting](#troubleshooting)
- [Project structure](#project-structure) · [Automated tests](#automated-tests)

## Commands

| Command | What it does |
|---|---|
| `/task add title [plan] [bucket] [labels] [assignee] [due_date] [description]` | Creates a task and replies with an embed: title, bucket, labels, assignees, due date, link. |
| `/task list [plan] [bucket] [assignee] [completed]` | Shows open (or completed) tasks. Only you see the reply. |
| `/task complete task [plan]` | Marks a task as completed. The notification names you as the person who completed it. |
| `/label create name [plan]` | Gives a name to an unused label of the plan, so it can be used with `/task add`. |

Details worth knowing:

- **plan**, **bucket**, **labels**, **assignee** and **task** autocomplete as you type. Buckets,
  labels and tasks follow the plan you picked (the default plan if you picked none).
- **labels** and **assignee** take one or more values separated by commas, for example
  `Bug, Frontend`. Autocomplete completes the entry you are typing and keeps the earlier
  ones. People are found by display name or email. A suggestion can hold at most 100
  characters (a Discord limit); longer lists can still be typed by hand.
- Labels are matched **by name**. A name that doesn't exist in the plan is an error that
  lists the available labels; nothing is created.
- Without a bucket, the task goes to `DEFAULT_BUCKET` if the plan has it, otherwise to the
  plan's first bucket (by Planner's order hint). The reply always shows the bucket used.
- **due_date** is `YYYY-MM-DD`.
- Only members with a role listed in `ALLOWED_ROLE_IDS` can use the commands or see
  suggestions. Mistakes and errors are answered privately, in plain words.

## How it works

**Authentication.** Planner's Graph endpoints accept app-only tokens (application
permission `Tasks.ReadWrite.All`), so by default the bot signs in as itself with a client
secret: nobody has to log in and there is no refresh token to expire. The alternative,
`AUTH_MODE=delegated`, makes the bot act as one signed-in user; see
[Delegated sign-in](#alternative-delegated-sign-in).

**Completion notifications use polling.** Planner is not among the resources that support
Microsoft Graph change notifications ([list of supported resources](https://learn.microsoft.com/en-us/graph/change-notifications-overview#supported-resources)),
so webhooks are not an option. Every `POLL_INTERVAL_SECONDS` (60 by default) the bot lists
the tasks of each plan in the group and compares them with the state it saved in SQLite:

- A task whose `percentComplete` has **become** 100 since the last look gets one message.
- The first look at a plan only records its state. Tasks that were already complete are
  never announced.
- Saving the new state and queueing the message happen in one database transaction, and a
  completion (task ID + completion time) can be queued only once. Restarts therefore don't
  repeat messages, and a completion that happened while the bot was down is announced once
  when it comes back.
- If Discord can't be reached, messages stay queued and are retried each round, for up to
  24 hours.
- A task that is reopened and completed again is announced again.

Expect a message up to one polling interval after the task was completed. `/task complete`
posts its notification immediately.

**Scope.** The bot works with the plans of one Microsoft 365 group: the group that owns
`DEFAULT_PLAN_ID`. It refuses plan, bucket and task IDs from anywhere else, even though the
app permission itself is tenant-wide. Only **basic** plans work: Premium plans are not
available through the Graph Planner API.

**Speed.** Plans, buckets, labels and group members are cached (`CACHE_TTL_SECONDS`, 300 by
default) and refreshed in the background, so autocomplete answers from memory within
Discord's three-second limit. A name that isn't in the cache triggers one fresh lookup
before it is reported as unknown.

**Graph errors.** 429 responses are retried after the `Retry-After` delay (and all other
requests wait too); a 412 ETag conflict makes the bot re-read the item and try again; a 401
makes it fetch a new token once.

## Setup

You need: a Discord server where you can add bots, admin rights in your Microsoft 365
tenant (to grant consent), and a machine that stays on. No inbound ports are needed; the
bot only makes outgoing HTTPS connections to Discord and Microsoft.

### 1. Discord application

1. Open the [Discord developer portal](https://discord.com/developers/applications) and choose **New Application**.
2. On the **Bot** page choose **Reset Token** and copy the token. This is `DISCORD_TOKEN`.
   Leave the three privileged gateway intents off; the bot doesn't need them.
3. On the **OAuth2** page, in the URL generator, tick the scopes **bot** and
   **applications.commands**, and the bot permissions **View Channels**, **Send Messages**
   and **Embed Links**. Open the generated URL and add the bot to your server.
4. In Discord, turn on **User Settings > Advanced > Developer Mode**. Then:
   - right-click the channel for completion messages > **Copy Channel ID** → `NOTIFY_CHANNEL_ID`
   - **Server Settings > Roles**, right-click each role that may use the bot > **Copy Role ID** → `ALLOWED_ROLE_IDS` (comma-separated)
5. If the notification channel is private, add the bot (or its role) to it.

To let every member use the bot, put the server's ID in `ALLOWED_ROLE_IDS`: it is also the
ID of the `@everyone` role.

### 2. Entra app registration

1. In the [Microsoft Entra admin center](https://entra.microsoft.com) open **App registrations > New registration**.
   Give it a name, keep **Accounts in this organizational directory only**, leave the
   redirect URI empty, and register.
2. From the **Overview** page copy **Application (client) ID** → `CLIENT_ID` and
   **Directory (tenant) ID** → `TENANT_ID`.
3. **API permissions > Add a permission > Microsoft Graph > Application permissions**, add:

   | Permission | Used for |
   |---|---|
   | `Tasks.ReadWrite.All` | Everything in Planner: plans, buckets, labels, tasks |
   | `GroupMember.Read.All` | Listing the group's members, for the assignee option |
   | `User.ReadBasic.All` | The names and emails of those members |

   Then choose **Grant admin consent**. All three must show *Granted*.

   `Group.Read.All` works in place of `GroupMember.Read.All`, but it also lets the app
   read group content such as conversations, in every group, which the bot never uses.
4. **Certificates & secrets > New client secret**. Copy the **Value** (not the Secret ID)
   right away → `CLIENT_SECRET`. Note the expiry date: when the secret expires, the bot
   logs "The client secret has expired" and can't reach Planner until you replace it.

`Tasks.ReadWrite.All` covers every plan in the tenant; Graph has no application
permission that is limited to one group's plans. The bot itself only touches the
configured group. If you'd rather have Microsoft enforce that limit, use delegated sign-in
with an account that is a member of just that group.

#### Alternative: delegated sign-in

The bot then acts as one user and can do exactly what that user can do in Planner.

1. Pick the account, ideally a dedicated service account. It must be a member of the
   group; check that it can open the plan in Planner itself.
2. In the app registration add **Delegated** permissions `Tasks.ReadWrite`,
   `Group.Read.All` and `User.ReadBasic.All`, and grant admin consent.
3. Under **Authentication**, set **Allow public client flows** to **Yes**.
4. Set `AUTH_MODE=delegated` in `.env` (`CLIENT_SECRET` is not used in this mode).
5. Sign in once:

   ```bash
   docker compose run --rm bot python auth.py login     # or: python auth.py login
   ```

   The command prints a short code and a Microsoft URL. Open the URL in any browser, enter
   the code, and sign in as the account from step 1. MSAL's token cache, including the
   refresh token, is saved as `msal_token_cache.json` in the data directory, readable only
   by the bot's user. From then on tokens are renewed silently.

You have to repeat the login if the account's password changes, its sessions are revoked,
a Conditional Access policy demands a fresh sign-in, or the bot was off for 90 days. The
log then says to run `python auth.py login` again; the bot picks up the new login without a
restart. If your tenant blocks the device-code flow, run `python auth.py login --browser`
on a PC (add the redirect URI `http://localhost` under *Mobile and desktop applications*
first) and copy the resulting `msal_token_cache.json` into the bot's data directory.
`python auth.py status` shows which account is signed in.

### 3. Planner IDs

Open the plan in Planner in a browser. Its address looks like

```
https://planner.cloud.microsoft/webui/plan/xqQg5FS2LkCp935s-FIFm2QAFkHM/view/board?tid=...
```

The 28 characters after `/plan/` are the plan ID → `DEFAULT_PLAN_ID`. The bot finds the
group from that plan, so `GROUP_ID` is optional.

If you can't get the ID from the address, set `GROUP_ID` to the group's **Object ID**
(Entra admin center > Groups) instead, leave `DEFAULT_PLAN_ID` empty and run
`check_setup.py` (step 5): it lists every plan of the group with its ID.

### 4. Configuration

```bash
cp .env.example .env
```

Fill in `DISCORD_TOKEN`, `NOTIFY_CHANNEL_ID`, `ALLOWED_ROLE_IDS`, `TENANT_ID`, `CLIENT_ID`,
`CLIENT_SECRET` and `DEFAULT_PLAN_ID`. Everything else has a sensible default; see the
[configuration reference](#configuration-reference). `.env` holds secrets: it is listed in
`.gitignore` and `.dockerignore` and is never copied into the Docker image.

### 5. Check the Microsoft 365 side

Before starting the bot, let it prove that the app registration works:

```bash
docker compose run --rm bot python check_setup.py     # or: python check_setup.py
```

The script signs in the way the bot will, shows the permissions in its token, and lists
the group's plans with their buckets and labels and the members that can be assigned. It
only reads. `FAIL` lines say what to fix.

## Run with Docker

```bash
docker compose build
docker compose run --rm bot python check_setup.py
docker compose up -d
docker compose logs -f bot
```

A healthy start logs `Registered 2 command group(s) in server ...`,
`Connected to Discord as ...`, `Microsoft Planner is reachable: N plan(s) in the group`,
and one `Now watching plan ...` line per plan.

- State lives in the named volume `planner-bot-data` (SQLite file, and the token cache in
  delegated mode). It survives `docker compose up -d --build` and restarts. If the volume
  is ever lost, the bot simply records a new baseline; nothing old is announced.
- `restart: unless-stopped` brings the bot back after a reboot. `docker compose stop`
  shuts it down cleanly.
- The container reports **healthy** while Planner has been checked successfully within the
  last three polling intervals (at least five minutes).
- To update after changing code or `requirements.txt`: `docker compose up -d --build`.
- On a NAS with a Docker interface (Synology Container Manager, QNAP Container Station,
  Portainer), create a project or stack from this folder; `docker-compose.yml`, the source
  files and `.env` have to stay together. If you bind-mount a host folder to `/data`
  instead of using the named volume, make it writable for user ID 10001.

## Run locally

Python 3.11 or newer.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python check_setup.py
python bot.py
```

State goes to `./data`. Stop with Ctrl+C.

## Run with systemd

`deploy/planner-bot.service` is a ready-made unit; the comment at its top lists the install
commands. It runs the bot as an unprivileged user from `/opt/planner-bot` and keeps the
state in `/var/lib/planner-bot`.

## Test checklist

Run these once after setup, in the Discord server, as a member with an allowed role.

| # | Do this | Expect |
|---|---|---|
| 1 | `/task add title: Test task` | A public embed with the title, the default bucket, "None" for labels and due date, "Unassigned", and a link. The task is in Planner, and the link opens it. |
| 2 | Start `/task add`, click **bucket** | The plan's buckets are suggested. Pick one that isn't the default: the embed and Planner both show the task in that bucket. |
| 3 | If the group has a second plan: pick that **plan** first, then **bucket** | The suggestions are that plan's buckets. |
| 4 | `/task add title: Label test labels: <an existing label>` | The embed shows the label; in Planner the task carries that label. |
| 5 | Two labels: type the first, then `, ` and the start of the second | Suggestions keep the first label and complete the second. Both end up on the task. |
| 6 | `/task add title: X labels: NoSuchLabel` | A private error naming the label and listing the available ones. No task is created. |
| 7 | `/label create name: Bot test`, then use it in `/task add` | The label appears in Planner's label list and on the new task. |
| 8 | `/task add title: Assignee test assignee: <a colleague's name>` | The colleague is suggested while typing and is assigned in Planner. Repeat with their email. |
| 9 | `/task add ... due_date: 2026-12-24 description: Some notes` | The due date shows in the embed and in Planner; the notes are in the task. `due_date: 24.12.2026` gives a private error. |
| 10 | In Planner, mark a task as completed | Within about a minute, a message in the notification channel: task title, who completed it, bucket, plan, link. |
| 11 | Wait two more minutes, then `docker compose restart bot` and wait again | No second message for that task. |
| 12 | Reopen the task in Planner, complete it again | A new message. |
| 13 | `/task complete task: <start typing>` | The task is suggested, gets completed, and the channel message shows you as the person who completed it. |
| 14 | `/task list` | A private list of the open tasks with bucket, due date and assignees. |
| 15 | As a member without an allowed role: `/task add` | A private refusal, and no suggestions while typing. |

## Configuration reference

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `DISCORD_TOKEN` | yes | | Bot token from the developer portal |
| `NOTIFY_CHANNEL_ID` | yes | | Channel for completion messages |
| `ALLOWED_ROLE_IDS` | in practice | *(nobody)* | Comma-separated role IDs that may use the commands. While it is empty nobody can use them. The server ID stands for `@everyone`. |
| `DISCORD_GUILD_ID` | | server of the channel | Server in which the commands are registered |
| `TENANT_ID` | yes | | Directory (tenant) ID |
| `CLIENT_ID` | yes | | Application (client) ID |
| `CLIENT_SECRET` | app mode | | Value of a client secret |
| `AUTH_MODE` | | `app` | `app` or `delegated` |
| `DEFAULT_PLAN_ID` | yes | | Plan used when a command names none; also identifies the group |
| `GROUP_ID` | | group of the default plan | Object ID of the Microsoft 365 group |
| `DEFAULT_BUCKET` | | first bucket | Bucket name or ID for tasks created without a bucket |
| `POLL_INTERVAL_SECONDS` | | `60` | How often Planner is checked (minimum 15) |
| `CACHE_TTL_SECONDS` | | `300` | Lifetime of cached plans, buckets, labels and members |
| `DATA_DIR` | | `data` (`/data` in Docker) | Where the SQLite state and token cache are kept |
| `LOG_LEVEL` | | `INFO` | `DEBUG` logs every Graph request |
| `TASK_URL_TEMPLATE` | | Planner web URL | Link format; placeholders `{plan_id}`, `{task_id}`, `{tenant_id}` |

If a value contains `$` or `#`, wrap it in single quotes.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| The commands don't show up in Discord | Look for `Registered 2 command group(s)` in the log. If it says the commands were not registered, the bot can't see `NOTIFY_CHANNEL_ID` or was invited without the `applications.commands` scope: re-invite it with the URL from step 1.3, then restart. Reload Discord (Ctrl+R) if the log looks fine. |
| "You don't have a role that is allowed..." | The member has none of the roles in `ALLOWED_ROLE_IDS`. After changing `.env`, restart the bot. |
| No suggestions while typing | Suggestions are empty for members without an allowed role, and for a few seconds after start-up. Otherwise run `check_setup.py`. |
| "Microsoft 365 didn't allow that" / `FAIL` with 403 | Admin consent is missing for one of the permissions, or (delegated mode) the signed-in account is not a member of the group. |
| "I can't sign in to Microsoft 365" | The log has the reason: wrong or expired client secret, wrong tenant or client ID, or (delegated) a login that has to be repeated. |
| Assignee names can't be found | `check_setup.py` reports members "without names": grant `User.ReadBasic.All` and admin consent. |
| "Completed by: Unknown" | The task was completed by someone who is not a member of the group. In app-only mode the bot can only read names of group members; grant `User.Read.All` as well if you want those names. |
| No completion messages | The log says why (`Completion notifications can't be posted: ...`). Usually the bot lacks View Channel, Send Messages or Embed Links in that channel. Queued messages are sent once it is fixed. |
| The link opens the plan but not the task | Microsoft changed Planner's web addresses. Copy a task link in Planner, compare it with `TASK_URL_TEMPLATE`, and set the variable to the new pattern. |
| `DEFAULT_PLAN_ID wasn't found` | A typo, or a Premium plan (its ID is a GUID). Only basic plans work. |
| Messages arrive late | They come up to `POLL_INTERVAL_SECONDS` after the change. If the log mentions throttling, Microsoft asked the bot to slow down and it will catch up. |

## Project structure

| File | Role |
|---|---|
| `bot.py` | Discord client, the slash commands, autocomplete, role check, embeds, error messages. Entry point. |
| `planner_service.py` | Planner logic: resolves names to IDs, creates and completes tasks, names label slots, caches lookups. |
| `graph_client.py` | Async Microsoft Graph client: tokens, 401/429/5xx handling, ETag errors, paging. |
| `auth.py` | MSAL token providers (app-only and delegated) and the `login` / `status` commands. |
| `notifier.py` | Polls Planner, detects completions, sends queued notifications. |
| `state.py` | SQLite storage: last-known task state and the notification outbox. |
| `matching.py` | Name matching and comma-separated list autocomplete. |
| `cache.py` | TTL cache with background refresh. |
| `config.py` | Reads and validates the environment variables. |
| `check_setup.py` | Read-only check of credentials, permissions, plans, labels and members. |
| `healthcheck.py` | Container health check. |
| `requirements.txt`, `.env.example` | Dependencies and the configuration template. |
| `Dockerfile`, `docker-compose.yml`, `deploy/planner-bot.service` | Deployment. |
| `tests/` | Automated tests with an in-memory stand-in for Microsoft Graph. |

## Automated tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests need no network, no tenant and no Discord server. `tests/fake_graph.py` imitates
the Graph endpoints the bot uses, including ETags, paging, throttling and expired tokens.
# Microsoft-Planner-Discord-Bot
