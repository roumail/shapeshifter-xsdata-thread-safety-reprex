# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "shapeshifter-uftp==2.4.0",
#     "xsdata[lxml]==26.10",
# ]
# ///
"""
Reprex: shapeshifter_uftp.transport.from_xml() is not thread-safe.

shapeshifter_uftp.transport keeps ONE module-level XmlParser / XmlContext that
every thread shares. To resolve the root element of an XML document
(e.g. <SignedMessage>) to a dataclass, xsdata calls
XmlContext.find_type() -> find_types() -> build_xsi_cache().

build_xsi_cache() (xsdata/formats/dataclass/context.py) does this, with no lock:

    if len(sys.modules) == self.sys_modules:   # (1) "cache is fresh" check
        return
    self.xsi_cache.clear()                     # (2) wipe the SHARED dict
    for clazz in self.get_subclasses(object):  # (3) walk EVERY class in the process
        ...
        self.xsi_cache[meta.target_qname].append(clazz)
    self.sys_modules = len(sys.modules)        # (4) mark fresh

Thread A finishes a rebuild and sets (4). Thread B, which started a little
later, then runs (2) and empties the dict while it walks (3). Any thread that
now parses passes check (1), looks the qname up in the half-built dict, finds
nothing, and gets:

    xsdata.exceptions.ParserError: No class found matching root: SignedMessage

(unseal_message() wraps the same error as SchemaException.)

When it bites:
  * the first parse(s) in a process, and again every time len(sys.modules)
    changes, i.e. after any import anywhere in the process;
  * when step (3) takes longer than the GIL switch interval (5 ms). Step (3)
    walks object.__subclasses__() recursively, so how long it takes depends on
    how many classes the whole app has loaded. A bare script is too small to
    hit it; a real service (FastAPI + SQLAlchemy + pandas + ...) is not.

Usage:
    uv run reprex.py           # shows the failure
    uv run reprex.py --lock    # same thing with a lock around from_xml: 0 failures
"""

import argparse
import collections
import sys
import threading
import time
import traceback
import types

arg_parser = argparse.ArgumentParser()
arg_parser.add_argument("--lock", action="store_true", help="serialize from_xml with a lock")
arg_parser.add_argument("--threads", type=int, default=8)
arg_parser.add_argument("--imports", type=int, default=60, help="how many times to import a new module")
arg_parser.add_argument("--io-ms", type=float, default=20, help="time a thread waits for its next request")
arg_parser.add_argument("--classes", type=int, default=30_000, help="extra classes to load (app size)")
args = arg_parser.parse_args()

# Stand-in for a real application's footprint, e.g. sqlalchemy + pandas imported
# before shapeshifter_uftp: ~30k classes, a ~25 ms rebuild. They are created
# first so that, as in that real case, the UFTP classes come late in the walk
# in step (3), and the dict stays without them for most of each rebuild.
_app_classes = [type(f"AppClass{i}", (), {}) for i in range(args.classes)]

from shapeshifter_uftp.transport import _context, from_xml, to_xml  # noqa: E402
from shapeshifter_uftp.uftp import SignedMessage  # noqa: E402

t0 = time.perf_counter()
_context.build_xsi_cache()
print(f"xsi_cache rebuild takes {(time.perf_counter() - t0) * 1000:.1f} ms "
      f"(GIL switch interval: {sys.getswitchinterval() * 1000:.0f} ms)")

xml = to_xml(SignedMessage(sender_domain="dso.example.com", sender_role="DSO", body=b"payload"))
full_cache_size = len(_context.xsi_cache)

# Diagnostics only (behaviour unchanged): note which threads are inside
# build_xsi_cache(), and when a lookup misses, snapshot the shared state.
XmlContext = type(_context)
_orig_build, _orig_find_types = XmlContext.build_xsi_cache, XmlContext.find_types
rebuilding = set()
miss_snapshots = []


def traced_build_xsi_cache(self):
    rebuilding.add(threading.get_ident())
    try:
        return _orig_build(self)
    finally:
        rebuilding.discard(threading.get_ident())


def traced_find_types(self, qname):
    result = _orig_find_types(self, qname)
    if not result and len(miss_snapshots) < 3:
        miss_snapshots.append(
            f"thread {threading.get_ident()} looked up {qname!r} and got nothing:\n"
            f"    cache marked fresh (sys_modules == len(sys.modules)): {self.sys_modules == len(sys.modules)}\n"
            f"    xsi_cache entries: {len(self.xsi_cache)} of {full_cache_size}\n"
            f"    other threads inside build_xsi_cache() right now: "
            f"{len(rebuilding - {threading.get_ident()})}"
        )
    return result


XmlContext.build_xsi_cache, XmlContext.find_types = traced_build_xsi_cache, traced_find_types

lock = threading.Lock()


def parse(message):
    if args.lock:
        with lock:
            return from_xml(message)
    return from_xml(message)


errors = collections.Counter()
first_traceback = {}
calls = 0
stop = threading.Event()


def handle_requests():
    """What each request-handling thread of a UFTP server does."""
    global calls
    while not stop.is_set():
        time.sleep(args.io_ms / 1000)  # waiting on the network for the next request
        calls += 1
        try:
            parse(xml)
        except Exception as exc:
            key = f"{type(exc).__module__}.{type(exc).__name__}: {exc}"
            errors[key] += 1
            first_traceback.setdefault(key, traceback.format_exc())


threads = [threading.Thread(target=handle_requests) for _ in range(args.threads)]
for t in threads:
    t.start()

# Meanwhile, something somewhere in the process imports a module, so
# len(sys.modules) changes and the next from_xml() rebuilds the cache.
for i in range(args.imports):
    sys.modules[f"_lazily_imported_{i}"] = types.ModuleType(f"_lazily_imported_{i}")
    time.sleep(0.1)

stop.set()
for t in threads:
    t.join()

print(f"\nlock={args.lock}: {sum(errors.values())}/{calls} from_xml() calls failed")
for key, count in errors.items():
    print(f"  {count:5d} x {key}")
for snapshot in miss_snapshots:
    print("\n" + snapshot)
for tb in first_traceback.values():
    print("\nFirst traceback:\n" + tb)

sys.exit(1 if errors else 0)
