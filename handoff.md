# Handoff: report to shapeshifter-library-python

Where to file: https://github.com/shapeshifter/shapeshifter-library-python/issues/new
Reprex repo: https://github.com/roumail/shapeshifter-xsdata-thread-safety-reprex

No existing issue covers this (searched "thread" in the shapeshifter repo, and
"thread" / "xsi_cache" / "concurrency" in tefra/xsdata, on 2026-10-17).
Related xsdata history: tefra/xsdata#1041 (an earlier thread-safety bug,
`warnings.catch_warnings`, fixed) and tefra/xsdata#1146 (`build_xsi_cache`
failing under ddtrace, a different cause).

---

## Draft issue

**Title:** `from_xml()` / `unseal_message()` is not thread-safe: intermittent "No class found matching root"

**Body:**

### Summary

Under concurrent load, `shapeshifter_uftp.transport.from_xml()` intermittently
raises

```
xsdata.exceptions.ParserError: No class found matching root: SignedMessage
```

and `unseal_message()` turns the same failure into `SchemaException`. As a
result, valid, correctly signed messages get rejected.

Minimal reproduction: https://github.com/roumail/shapeshifter-xsdata-thread-safety-reprex
(`uv run reprex.py` fails in most runs; `uv run reprex.py --lock` never does).

Versions: shapeshifter-uftp 2.4.0, xsdata 26.10, CPython 3.13 / 3.14 (with the GIL).

### Cause

`transport.py` creates a single `XmlContext` / `XmlParser` at module level that
every request thread shares:

```python
_context = XmlContext()
serializer = XmlSerializer(context=_context, config=SerializerConfig(indent="  "))
parser = XmlParser(context=_context)
```

`from_xml()` calls `parser.from_string(message)` / `from_bytes(message)` without
a target class, so xsdata resolves the root element through
`XmlContext.find_type()` → `build_xsi_cache()`. That method clears the shared
`xsi_cache` dict and refills it by walking every class in the process, with no
locking. It also checks freshness with `len(sys.modules)`, so the rebuild
happens again whenever *any* module is imported anywhere in the process, not
only on the first parse. If one thread marks the cache fresh while another
has just cleared it, a third thread's lookup finds nothing.

The window is wide enough to hit when the process has many classes loaded,
as a typical service does. In a small test script the rebuild finishes
within one GIL time slice and the bug stays hidden.

### Suggested fix

Serialize parsing in `transport.py`:

```python
import threading

_parser_lock = threading.Lock()

def from_xml(message: str | bytes):
    """
    Parse the given message string into a Shapeshifter UFTP object.
    """
    with _parser_lock:
        if isinstance(message, str):
            return parser.from_string(message)
        if isinstance(message, bytes):
            return parser.from_bytes(message)
    raise TypeError(f"Message should be either bytes or str, not {type(message)}")
```

Parsing these small messages is CPU-bound and holds the GIL for most of its
run anyway, so the lock costs very little. The root cause is in xsdata (`build_xsi_cache` should build a
new dict and swap it in, or take a lock), and we plan to report that upstream
as well; the lock above stays correct after that fix.

Happy to open a PR with the lock and a regression test if that helps.
