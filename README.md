# shapeshifter-uftp / xsdata thread-safety reprex

`shapeshifter_uftp.transport.from_xml()` can fail under concurrency with:

```
xsdata.exceptions.ParserError: No class found matching root: SignedMessage
```

(`unseal_message()` reports the same failure as `SchemaException`.)

## Run

Requires [uv](https://docs.astral.sh/uv/); dependencies are declared inline in the script.

```sh
uv run reprex.py          # fails (exit 1) in most runs; it is a race
uv run reprex.py --lock   # same workload with a lock around from_xml: never fails
```

Tested with shapeshifter-uftp 2.4.0 and xsdata 26.10, Python 3.13 / 3.14.

## What happens

`shapeshifter_uftp.transport` shares one module-level `XmlParser` / `XmlContext`
across all threads. To map the root element to a dataclass, xsdata calls
`XmlContext.find_type()` → `build_xsi_cache()`
([context.py](https://github.com/tefra/xsdata/blob/main/xsdata/formats/dataclass/context.py)),
which, without a lock:

1. returns early if `len(sys.modules) == self.sys_modules` ("cache is fresh"),
2. otherwise **clears the shared `xsi_cache` dict**,
3. refills it by walking every class in the process (`object.__subclasses__()`, recursively),
4. sets `self.sys_modules = len(sys.modules)`.

If thread A finishes its rebuild and marks the cache fresh while thread B has
just cleared the dict, any thread that parses next passes step 1, finds the
qname missing from the half-filled dict, and the parse fails.
The script's diagnostics capture exactly that state at the moment of a miss:

```
thread 6252802048 looked up 'SignedMessage' and got nothing:
    cache marked fresh (sys_modules == len(sys.modules)): True
    xsi_cache entries: 81 of 154
    other threads inside build_xsi_cache() right now: 3
```

## Why it is easy to miss in tests

- The rebuild happens on the first parse **and again every time `len(sys.modules)`
  changes**, i.e. after any import anywhere in the process. Warming the cache
  at startup does not help.
- Step 3 takes time in proportion to the number of classes loaded in the
  process. In a bare script it takes about 3 ms, shorter than the GIL switch
  interval (5 ms), so it practically never interleaves. In a real service
  (FastAPI, SQLAlchemy, pandas, ...) it takes 25 ms or more. The script creates
  30k placeholder classes to stand in for that.
- Threads released together by a barrier all rebuild in turn, and that
  interleaving is mostly safe. Failures come from requests arriving at
  different times, so each worker waits 20 ms between requests, as if waiting
  on the network.
