# Regression tests for xsdata (work in progress)

Patches against [tefra/xsdata](https://github.com/tefra/xsdata) `main` at
`33b9558` (2026-10-17):

- `xsdata-tests-only.patch` adds two tests to
  `tests/formats/dataclass/test_context.py`. Both fail on current `main`.
- `xsdata-fix-and-tests.patch` is the same tests plus a candidate fix to
  `XmlContext.build_xsi_cache()`, which builds the new cache in a local dict
  and swaps it in.

```sh
git clone https://github.com/tefra/xsdata && cd xsdata
git apply ../xsdata/xsdata-tests-only.patch
pytest tests/formats/dataclass/test_context.py -k "rebuild"
```

## The tests

They don't depend on timing. Each one patches `XmlContext.get_subclasses`
(and in the second test, `build_xsi_cache`) to pause named threads at fixed
points, which forces the interleaving from the shapeshifter reprex every run.

1. `test_find_type_while_another_thread_rebuilds_xsi_cache`: two threads
   rebuild concurrently. A finishes its walk, B clears the shared dict, A marks
   the cache fresh and looks up the qname. Then the main thread looks up while
   B is still walking.
2. `test_find_type_while_a_new_import_triggers_a_rebuild`: A has passed the
   freshness check, a module is imported, B starts a rebuild, and A then
   looks up while B is still walking.

## Results (10 runs each)

| `build_xsi_cache` implementation                       | test 1 | test 2 |
|--------------------------------------------------------|--------|--------|
| current `main`                                         | fails  | fails  |
| global lock around the rebuild                         | fails  | fails  |
| lock + re-check freshness, still clears dict in place  | passes | fails  |
| build a new dict, then swap it in (the patch)          | passes | passes |

With the patch, the full xsdata suite passes: 1119 passed, 17 skipped.
`ruff` and `mypy` are clean.

The swap fixes these two cases. Two threads may still rebuild at the same
time, which wastes some work but gives the same result. `local_names_match()`
still removes entries from `xsi_cache` in place, but that is not new.
