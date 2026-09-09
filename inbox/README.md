# Inbox

This directory is how CRUX accepts work without growing a GitHub issue
thread and without ever putting a large payload in an Actions environment
variable.

## Submit a block, a transaction, or an identity

1. Run the miner or wallet. It writes a file here, one line, a few
   kilobytes even at the highest difficulty.
2. Open a pull request that **adds exactly that file and changes nothing
   else**.
3. The node reads the file through the API as inert text, validates it,
   applies it to `main`, and closes the pull request. It is never merged,
   so concurrent submissions cannot conflict and no code from your fork
   ever runs with a write token.

Alternatively, open a **new** GitHub issue whose body is the same line,
labelled `crux`. The node processes it and closes the issue. Do not
comment on an old issue — standing threads are how payloads and Actions
logs blow up.

Files in this directory are never merged, so it stays empty aside from
this README.
