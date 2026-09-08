---
name: pending
description: List proposals waiting for human approval, with thesis, sizing and the commands to approve or reject.
allowed-tools: Bash(./bin/tradeagent *) Bash(uv run tradeagent *)
---

```!
./bin/tradeagent pending
```

For each pending proposal above, give the user a compact card: id, summary line, thesis in one sentence, the
strongest bear point, sizing (max qty, notional), and the two commands:

```
uv run tradeagent approve <id>
uv run tradeagent reject <id> "reason"
```

Then say: after approving, run `/execute` to place. If there are none, say so in one line.
Use `./bin/tradeagent show <id>` only if the user asks for the full proposal.
