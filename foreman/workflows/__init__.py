"""One graph per kind of work. The router picks the key; each module's `build(run)` returns the graph."""
from foreman.workflows import bugfix, existing_change, doc_improvement, new_implementation, observe, spike, test_improvement

GRAPHS = {
    "new_implementation": new_implementation.build,
    "existing_change": existing_change.build,
    "bugfix": bugfix.build,
    "test_improvement": test_improvement.build,
    "doc_improvement": doc_improvement.build,
    "spike": spike.build,
    "observe": observe.build,
}
