# Internets Engineering Knowledge Base

Complete engineering documentation for the Internets IRC bot and multi-provider
weather aggregator. This is not a user guide - it is the system knowledge
corpus for maintaining, extending, debugging, securing, deploying, and
operating the software. Written so that a team of experienced engineers with
no prior knowledge of this codebase can assume ownership without relying on
institutional memory.

- **Platform support:** Linux, macOS, FreeBSD, Windows, WSL/WSL2, Cygwin, MinGW, MSYS2
- **Python:** 3.10+ (CI runs 3.10 through 3.14)
- **License:** ISC
- **Source:** <https://github.com/brandontroidl/Internets>

## Where to start

| You are | Read |
|---|---|
| New to the project | [executive](executive.md), then [getting-started](getting-started.md) |
| Running it | [deployment](deployment.md), [operations](operations.md), [troubleshooting](troubleshooting.md) |
| Administering it | [administration](administration.md), [command-reference](command-reference.md) |
| Reviewing its security | [security-model](security-model.md), [known-issues](known-issues.md), then [internals](internals/index.md) |
| Extending it | [writing-modules](writing-modules.md), [writing-providers](writing-providers.md) |
| Inheriting it | [handoff](handoff.md), [knowledge-recovery](knowledge-recovery.md) |
| Changing the code | [internals](internals/index.md) for the file you are touching |

```{toctree}
:maxdepth: 2
:caption: I. Executive

executive
```

```{toctree}
:maxdepth: 2
:caption: II. Architecture

architecture
irc-protocol
state-and-persistence
```

```{toctree}
:maxdepth: 2
:caption: III. Security

security-model
logging-and-auditing
```

```{toctree}
:maxdepth: 2
:caption: IV. Operations

deployment
configuration
operations
administration
troubleshooting
metrics-and-observability
integrations
```

```{toctree}
:maxdepth: 2
:caption: V. Development

getting-started
command-reference
modules
writing-modules
providers
writing-providers
testing
contributing
```

```{toctree}
:maxdepth: 2
:caption: VI. Design Decisions

design-decisions
```

```{toctree}
:maxdepth: 2
:caption: VII. Handoff

handoff
knowledge-recovery
```

```{toctree}
:maxdepth: 2
:caption: VIII. Project

known-issues
changelog
security-policy
```

```{toctree}
:maxdepth: 2
:caption: IX. Implementation Reference

internals/index
```

```{toctree}
:maxdepth: 2
:caption: X. API Reference

autoapi/index
```
