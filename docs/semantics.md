# What a report establishes

Sentinels inspects source with `ast` and `symtable`. It does not execute the
application. The configured production entry, selected source roots, excluded
files, and supported adapter semantics bound the result. The report records
that scope and the relevant source witness.

`PASS` means the declared structural requirement was established in the
supported analysis. `FAIL` means there is a definite violation. `UNRESOLVED`
means relevant analysis cannot establish or reject the requirement. `ERROR`
means the selected input or configuration is invalid. Individual findings remain
visible when several statuses coexist. The analyzer does not promise that all
branches execute successfully or that external systems accept a publication.

## Binding and values

Import aliases and lexical bindings are resolved before an edge is accepted.
Parameters and local assignments can shadow imports; a matching spelling alone
is insufficient. The value-flow analysis follows supported assignments and
aliases and preserves producer identity. Overwriting a binding ends its previous
value's flow. A field on a different value does not satisfy a handoff.

Ordinary, known receivers can connect calls to method bodies. Inheritance,
overrides, `self`, `cls`, static/class methods, properties, and `super()` require
the supported binding evidence. An annotation establishes a declared interface;
it does not select a concrete runtime implementation. Ambiguous or opaque
dispatch is reported when it affects a requirement. An unawaited async call does
not establish execution of the coroutine body.

Reflection, monkeypatching, dynamic decorators, metaclasses, custom descriptors,
and analysis limits can prevent resolution. Unknown code outside a proven
requirement's witness does not invalidate that proof.

Generators and comprehensions are bounded limitations in v0.1.0. Creating a
generator does not run its body; unsupported iteration or comprehension
evaluation receives unresolved value/dispatch evidence. Constant boolean
short-circuiting and conditional expressions preserve unreachable branches.
Module and class member replacement prevents reuse of obsolete function bodies.

## Frameworks

Prefect adapters distinguish direct invocation, task submission/mapping,
configured task copies, and explicit `.fn()` bypass. Futures can carry payloads
to downstream tasks. Waiting on a future, or specifying `wait_for`, supplies
ordering evidence without supplying a result to the consumer. Scheduling an
invocation establishes a structural connection, not successful execution.

Typer adapters connect commands and callbacks to the owning application.
Registration matters only when that application is attached to the configured
invoked root. Unattached subapplications are disconnected; callback replacement
must respect the final registration. Registration does not prove a command was
selected by actual arguments.

## Shape checks

Literal mappings and export sets can provide definite field evidence. Different
return paths are checked separately; their keys are not unioned into a fictitious
complete payload. JSON artifact values are checked using JSON types, including
the distinction between booleans and integers.

Pydantic field evidence is inspected from source, including supported inherited
fields, defaults, optional presence, nullability, and literal aliases. Supported
`model_dump` projections are checked against their effective fields. Dynamic
schemas, alias generators, custom serializers, and conditional projections can
invalidate shape resolution. A static field declaration does not establish that
runtime model validation occurred.

Literal defaults contribute actual output-value evidence for a supported model
construction. Dynamic factories and field-level serialization controls can
prevent resolution. The checker does not reproduce Pydantic coercion, validators,
or runtime default validation.

## Runtime counterexample

The lab includes a fixed, separately labeled execution observation showing a
branch that bypasses an otherwise connected validator. Editing a source example
reruns static analysis only. The fixed observation is never presented as
execution of a visitor's edited program.
