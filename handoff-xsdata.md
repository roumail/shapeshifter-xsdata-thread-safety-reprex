# Handoff: report to tefra/xsdata

Where to file:
- Issue: https://github.com/tefra/xsdata/issues/new
- PR: fork tefra/xsdata, branch from `main`, apply
  [`xsdata/xsdata-fix-and-tests.patch`](xsdata/xsdata-fix-and-tests.patch)
  (made against `33b9558`, 2026-10-17), open the PR against `main`, and
  replace `#xxxx` with the issue number.

Reprex repo: https://github.com/roumail/shapeshifter-xsdata-thread-safety-reprex

No existing issue covers this (searched "thread", "thread safe",
"concurrency" and "xsi_cache" on 2026-10-17). Related: #1041 (an earlier
thread-safety bug, `warnings.catch_warnings`, fixed) and #1146
(`build_xsi_cache`, a different cause).

Before opening the PR: the xsdata-samples and W3C test suites from the
template checklist have **not** been run. Only xsdata's own test suite has.

---

## Draft issue

**Title:** `XmlContext.find_type()` can return `None` for a known type while another thread rebuilds `xsi_cache`

**Body:**

### Summary

When one `XmlContext` (or `XmlParser`) is shared between threads, parsing a
document without passing `clazz` intermittently fails with

```
xsdata.exceptions.ParserError: No class found matching root: SignedMessage
```

even though the class is imported and is found on every single-threaded run.

We found this through [shapeshifter-uftp](https://github.com/shapeshifter/shapeshifter-library-python),
which keeps one module-level `XmlParser(context=XmlContext())` for all request
threads. Reproduction: https://github.com/roumail/shapeshifter-xsdata-thread-safety-reprex

Versions: xsdata 26.10 (also on `main` at `33b9558`), CPython 3.13 / 3.14 with the GIL.

### Cause

`XmlContext.build_xsi_cache()`:

```python
if len(sys.modules) == self.sys_modules:   # (1) cache is fresh
    return
self.xsi_cache.clear()                     # (2) clears the shared dict
for clazz in self.get_subclasses(object):  # (3) walks every class in the process
    ...
    self.xsi_cache[meta.target_qname].append(clazz)
self.sys_modules = len(sys.modules)        # (4) marks it fresh
```

Two ways this goes wrong:

1. Threads A and B both rebuild. A finishes step 3, then B clears the dict in
   step 2, then A marks the cache fresh. From then on, any lookup passes
   step 1 and reads B's half-built dict until B finishes.
2. Thread A passes step 1. Then a module is imported somewhere, B starts a
   rebuild and clears the dict, and A looks up the qname in the half-built dict.

Freshness is checked with `len(sys.modules)`, so a rebuild happens not only on
the first parse but after **any** import anywhere in the process, so warming
the cache at startup doesn't avoid it. Step 3 takes time in proportion to the
number of classes loaded. In a small script it finishes within one GIL time
slice (5 ms) and the bug rarely shows. In an application with FastAPI,
SQLAlchemy, pandas and so on loaded, it takes 25 ms or more and the window is
easy to hit.

### Suggested fix

Build the new cache in a local dict and assign it to `self.xsi_cache` only when
it is complete. Readers then see either the old complete cache or the new
complete cache, never a cleared one. A PR follows with the change and
regression tests for both cases above.

Adding a lock around the rebuild is not enough on its own, because readers
that see the cache as fresh never take the lock, and they read the same dict
that the rebuild clears (see the PR's test results).

---

## Draft PR

**Title:** Make `XmlContext.build_xsi_cache` safe for concurrent readers

**Body:**

## 📒 Description

`build_xsi_cache()` cleared the shared `xsi_cache` dict and refilled it in
place. Another thread that saw the cache as fresh could look a type up in the
half-built dict and get `None`, so parsing failed with
`ParserError: No class found matching root: ...`.

Resolves #xxxx

## 🔗 What I've Done

- `build_xsi_cache()` builds the new cache in a local dict and assigns it to
  `self.xsi_cache` when complete, so readers only ever see a complete cache.
- `sys_modules` is now read before the walk instead of after it. If a module
  is imported during the walk, its classes may have been missed, so the next
  call should rebuild again. Reading the count afterwards would mark that
  incomplete cache as fresh.
- Added two tests in `tests/formats/dataclass/test_context.py`, one for each
  interleaving in the issue. They don't depend on timing: they patch
  `get_subclasses` (and `build_xsi_cache` in the second test) to pause named
  threads at fixed points, so the race happens on every run.

## 💬 Comments

How the tests separate the possible fixes (10 runs each):

| `build_xsi_cache` implementation                  | test 1 | test 2 |
|---------------------------------------------------|--------|--------|
| current `main`                                    | fails  | fails  |
| lock around the rebuild                           | fails  | fails  |
| lock + re-check freshness, still clears in place  | passes | fails  |
| build a new dict, then swap it in (this PR)       | passes | passes |

Two threads can still rebuild at the same time. That wastes some work but
both produce the same result, and it avoids a lock on a path every parse
takes. `local_names_match()` still removes entries from `xsi_cache` in place;
I left that unchanged.

The tests depend on `get_subclasses` and `build_xsi_cache`. If you would
rather restructure the cache (for example behind a lock and an immutable
mapping), I'm happy to rework them.

## 🛫 Checklist

- [ ] Updated docs
- [x] Added unit-tests
- [ ] [Sample tests](https://github.com/tefra/xsdata-samples) pass
- [ ] [W3C tests](https://github.com/tefra/xsdata-w3c-tests) pass

Local run: full test suite 1119 passed, 17 skipped; `ruff check`, `ruff format`
and `mypy` are clean.
