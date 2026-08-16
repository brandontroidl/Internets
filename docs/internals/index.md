# Implementation reference

The implementation layer of the documentation. Where the guides in the rest of
this corpus explain the system at the level an operator or integrator needs,
these pages explain the code itself: what each file does, why it exists, what
state it touches, how it fails, and what would break if its behavior changed.

Each page follows the same structure - purpose, responsibilities and boundaries,
dependencies and dependents, lifecycle, state, concurrency, failure behavior,
security, then per-class and per-function detail, an implementation walk, and a
findings section. The findings sections are deliberate: where the implementation
looks questionable or defective, the page says so rather than inventing a
rationale for it. Consolidated findings live in the reconstruction report.

Citations are symbol-primary (`file.py - Class.method()`) because line numbers
rot on the next edit. Where a line number appears it is secondary navigation
only, and no claim rests on one.

## Execution core

- [internets](internets.md) - IRCBot: connection lifecycle, dispatch, module loading, startup and restart
- [admin_cmds](admin_cmds.md) - AdminCommandsMixin: every core and admin command handler
- [sender](sender.md) - outbound pipeline: priority queue, token bucket, serialization
- [protocol](protocol.md) - IRC line parsing and ISUPPORT/MODE/NAMES primitives
- [console](console.md) - interactive stdin console on a daemon thread

## Configuration, secrets, identity

- [config](config.md) - config.ini + local overlay parsing, reload semantics, frozen constants
- [secret_store](secret_store.md) - two-tier secret resolution and permission enforcement
- [hashpw](hashpw.md) - admin password hashing and verification
- [botlog](botlog.md) - logging setup, per-subsystem debug, hash validation

## State, durability, observability

- [store](store.md) - persistent datasets, atomic writes, corruption quarantine, rate limiting
- [audit_log](audit_log.md) - privileged-action audit trail and its hash chain
- [process_lock](process_lock.md) - single-instance lock and stale-PID handling
- [metrics](metrics.md) - metric registry and the exposition endpoint

## Extension layers

- [modules/index](modules/index.md) - the command module system and one page per module
- [weather-providers/index](weather-providers/index.md) - the weather aggregation layer and one page per provider

## Verification and build

- [tests](tests.md) - the test suite as behavioral evidence, plus the honest gap inventory
- [ci-and-packaging](ci-and-packaging.md) - workflows, packaging, dependency policy, scripts

```{toctree}
:hidden:
:maxdepth: 1

internets
admin_cmds
sender
protocol
console
config
secret_store
hashpw
botlog
store
audit_log
process_lock
metrics
tests
ci-and-packaging
modules/index
weather-providers/index
```
