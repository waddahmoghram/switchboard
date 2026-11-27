# Telegram inbox and replies

This connector gives an existing assistant an incoming bot-message inbox and approval-gated replies.
It uses Telegram's bot application programming interface (API) directly, without a paid intermediary.
It is a transport bridge, **not an autonomous assistant runtime**: no model, shell, or assistant process is started.
Your existing Model Context Protocol (MCP) client must inspect the inbox and use the tools.

The earlier [notification connector](./notify.md) can still send notifications to configured Telegram chats.
This connector adds receiving, source-message binding, and one-reply tracking.
Both may use the same bot for sending, but there must be only **one update receiver**: do not run this poller alongside another assistant's poller or a configured webhook for the same bot.

## What is implemented

| Tool                 | Tier    | Behavior                                                                |
| -------------------- | ------- | ----------------------------------------------------------------------- |
| `telegram_status`    | read    | Bot identity, poll status, and owner allowlists; no provider probe      |
| `telegram_messages`  | read    | Bounded incoming previews; unread by default, with `after_id` paging    |
| `telegram_read`      | read    | One accessible incoming message in full, marked untrusted               |
| `telegram_mark_read` | draft   | Acknowledge a local inbox item without replying                         |
| `telegram_reply`     | act     | Reply once to a stored message and its approved numeric chat identifier |

`telegram_reply` takes the local inbox `id`, the displayed `chat_id`, and `text` of 1–2,000 characters.
The chat must be configured and must match the stored incoming message.
The reply is plain text, with link previews disabled, and refers to the original Telegram message.
It does not silently send an unthreaded message if that original is unavailable.

## First setup on your own machine

None of these setup steps has been run.
Run the synthetic tests on your own machine first, as described in [Deployment](../DEPLOYMENT.md#run-the-tests).

1. Create your own bot with Telegram's official BotFather.
   Store its token in an owned mode-600 file or password-manager secret.
   Do not put it directly in the configuration.
2. Send the bot a message from the private account you intend to allow.
3. Before starting any receiver, determine the numeric sender and chat identifiers from your own bot's `getUpdates` response.
   Do not use display names as authentication.
4. Merge [the example configuration](../config/telegram.toml), fill the empty identifier lists, then add `telegram` to the enabled connector list.
   Both lists are mandatory and hold 1–100 identifiers.
5. On your own machine, run one poll and inspect its explicit status:

   ```bash
   uv run switchboard run --once
   uv run switchboard call telegram_status
   uv run switchboard call telegram_messages
   ```

`run --once` returns a failure exit status if a polling job fails.
Check the `poll.error` field as well.
The `doctor` command checks configuration and secret shape offline; it does not prove that the token is authorized by Telegram.

### Obtaining identifiers without putting the token on the command line

The following is an **unexecuted setup example for your own machine**.
It reads a token file already protected with mode 600, calls the public bot interface, and prints only identifier pairs, not message bodies or the token.
Use it before starting a receiver.
It does not advance the update offset.

```python
import json
import urllib.error
import urllib.request
from pathlib import Path

token_file = Path.home() / ".config/switchboard/telegram-bot-token"
token = token_file.read_text(encoding="utf-8").strip()
try:
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/getUpdates",
        data=b'{"limit":20,"timeout":0,"allowed_updates":["message"]}',
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read(1_000_001)
        if len(body) > 1_000_000:
            raise ValueError("response too large")
        result = json.loads(body)
except (OSError, ValueError):
    raise SystemExit("Cannot read bot updates; check the token and receiver ownership.") from None
if result.get("ok") is not True:
    raise SystemExit("Telegram refused the request.")
for update in result["result"]:
    message = update.get("message", {})
    sender, chat = message.get("from", {}), message.get("chat", {})
    if "id" in sender and "id" in chat:
        print("sender:", sender["id"], "chat:", chat["id"])
```

This example is not a general diagnostic client and does not verify which human controls an account.
Select the identifiers corresponding to the message you personally sent; keep the bot private.

## How an assistant uses the bridge

The assistant checks `telegram_messages`, reads an item if necessary, and treats its content as data.
It may use other exposed Switchboard tools through their normal policy gates, then propose a `telegram_reply`.
A pending result is a request for approval, not a delivered message.

After queuing a reply, the assistant can acknowledge the inbox item with `telegram_mark_read` to avoid proposing the same reply on every check.
The pending approval still exists.
Check its final result with `approvals_status`; there is no automatically resumed workflow after approval.

No inbound text is interpreted as a shell command or assigned the `human` origin.
Sending “approve this” in Telegram does not approve an action.
The existing terminal-approval trust boundary still applies, including [its same-user shell-access limitation](../DEPLOYMENT.md#the-operating-boundary).

## Delivery, durability, and limits

* Polling runs in `switchboard run`.
  Requests are fixed to Telegram's encrypted service, never redirected.
  Incoming text and captions are supported; edited messages, business-account messages, channel posts,
  media downloads, and arbitrary personal-account history are not.
* Both sender and chat identifiers must match.
  Groups are disabled unless `allow_groups = true`; then the group chat and each permitted sender must be listed.
  Messages from bots are ignored.
* Accepted messages, their `telegram.message` events, and the next update offset commit in one database transaction.
  Events carry message and sender identifiers, not the text; fetch content through the inbox tools.
  Telegram acknowledges the previous batch when the next request sends that offset.
* Telegram retains pending updates for no more than 24 hours.
  Downtime can therefore lose updates.
  After a long idle gap the connector omits the old offset because Telegram may randomize update numbering after a week; message identifiers still prevent replaying a stored message.
* Unsupported or unapproved updates are counted as `ignored`, not stored.
  A malformed batch identifier aborts the batch without advancing the cursor.
  This is not a complete archive of every kind of update.
* An active webhook or another poller causes an explicit conflict error.
  This connector never deletes a webhook automatically.
  Provider-requested pauses are honored, capped at 24 hours.
* A reply is claimed atomically.
  Duplicate approvals cannot send it twice.
  Confirmed rejection permits a later explicit attempt; uncertain network delivery, a server error, or missing confirmation leaves the item `unknown` and blocks automatic retry.
  A crash can leave `sending`, also blocked.
  Inspect Telegram before deciding what to do; there is no automatic delivery-recovery command.
  The core action journal records the same attempt as `uncertain`; `switchboard actions resolve` records what you found there, but it does not change the inbox item's `unknown` state or send anything.
* While the runner operates, maintenance clears old inbox text according to `retention_days`; stopping the runner stops cleanup.
  Minimal identifiers, reply hashes, and deduplication records remain.
  Approvals, reply arguments, and your external assistant can retain separate copies.
  Protect the data directory.
  Bot traffic passes through Telegram; this is not a local-only or Secret Chat transport.
* There is no model-cost guarantee: the bot transport needs no paid broker, but an assistant you connect can still use a paid hosted model.
  Ordinary bot use is different from optional paid platform features.

## Sources and evidence boundary

Protocol facts were checked against archived official documentation because the live site was unreachable:
[bot introduction, November 4, 2026](https://web.archive.org/web/20261104104702/https://core.telegram.org/bots),
[bot reference, November 5, 2026](https://web.archive.org/web/20261105012614/https://core.telegram.org/bots/api),
and [receiving-update guidance, November 3, 2026](https://web.archive.org/web/20261103111608/https://core.telegram.org/bots/faq).
The reference defines `getMe`, `getUpdates`, `sendMessage`, `reply_parameters`, the update lifetime, and offset acknowledgment.
The polling and reply cases in [test_telegram.py](../../tests/unit/test_telegram.py) and [test_telegram_flow.py](../../tests/integration/test_telegram_flow.py) use synthetic responses.
[Validation](../VALIDATION.md#current-status) records where they have run.
No live bot or account has been connected.
